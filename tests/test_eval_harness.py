"""Tests for the routing eval harness itself.

These pin the harness's *plumbing* — loader, validator, scorer, CLI
glue — against a tiny synthetic fixture written into a temp dir.
Tests deliberately do NOT exercise the committed
``evals/routing/scenarios.json``: that file is the production
benchmark, not test data, and its contents will churn whenever PR-2/3
relabels scenarios.

Coverage targets:

1. ``load_scenarios`` reads JSON, validates per-scenario, raises on
   malformed fixtures.
2. ``spearman`` returns the expected closed-form result for tiny
   inputs and ``None`` for the undefined-correlation cases (n<2,
   mismatched sets).
3. ``evaluate`` aggregates correctly: top-1 accuracy is fraction
   matched, mean Spearman skips ``None`` scenarios.
4. CLI ``main`` writes ``baseline.json`` to the path it was given
   and exits 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agora.evals.routing import (
    EvalReport,
    Scenario,
    ScenarioResult,
    _check_floor,
    evaluate,
    load_scenarios,
    main,
    spearman,
)
from agora.models.candidate import HolderCandidate

# Two-scenario synthetic fixture — one rules-baseline matches, one it
# inverts. Enough to exercise top-1 accuracy and the Spearman aggregate
# without coupling the test to the real eval set.
_SYNTHETIC_SCENARIOS: list[dict[str, object]] = [
    {
        "id": "synth-001",
        "description": "Trivial: single consortium available holder.",
        "candidates": [
            {
                "symbol": "MEM-A",
                "is_consortium_member": True,
                "status": "available",
                "preferred_score": 0.5,
            }
        ],
        "expected_chosen": "MEM-A",
        "expected_ranking": ["MEM-A"],
        "notes": "single-candidate; Spearman undefined.",
    },
    {
        "id": "synth-002",
        "description": "Two-candidate sanity: consortium beats external.",
        "candidates": [
            {
                "symbol": "EXT",
                "is_consortium_member": False,
                "status": "available",
                "preferred_score": 0.5,
            },
            {
                "symbol": "MEM-A",
                "is_consortium_member": True,
                "status": "available",
                "preferred_score": 0.5,
            },
        ],
        "expected_chosen": "MEM-A",
        "expected_ranking": ["MEM-A", "EXT"],
        "notes": "rules + ground truth agree.",
    },
]


def _write_synthetic(tmp_path: Path) -> Path:
    """Drop the synthetic set into a temp file and return its path."""
    p = tmp_path / "scenarios.json"
    p.write_text(json.dumps(_SYNTHETIC_SCENARIOS), encoding="utf-8")
    return p


# --- spearman --------------------------------------------------------------


def test_spearman_identical_ranking_is_one() -> None:
    """Identical ranks → rho = 1.0 (closed-form check)."""
    assert spearman(["a", "b", "c"], ["a", "b", "c"]) == 1.0


def test_spearman_reversed_ranking_is_minus_one() -> None:
    """Fully reversed ranks → rho = -1.0 (closed-form check)."""
    assert spearman(["a", "b", "c"], ["c", "b", "a"]) == -1.0


def test_spearman_single_element_is_none() -> None:
    """n<2: correlation undefined; harness contract returns None so the
    scenario is skipped from the aggregate, not silently scored 0."""
    assert spearman(["a"], ["a"]) is None
    assert spearman([], []) is None


def test_spearman_mismatched_sets_returns_none() -> None:
    """Caller bug: ranking lists don't cover the same symbols. Surface
    as None rather than silently zero."""
    assert spearman(["a", "b"], ["a", "c"]) is None


def test_spearman_length_mismatch_returns_none() -> None:
    """Different list lengths → None (line 215 early return)."""
    assert spearman(["A"], ["A", "B"]) is None


def test_spearman_partial_inversion() -> None:
    """One swap in a 4-element list. d² = 1+1+0+0 = 2 →
    rho = 1 - 6*2 / (4 * 15) = 1 - 12/60 = 0.8."""
    assert spearman(["a", "b", "c", "d"], ["b", "a", "c", "d"]) == pytest.approx(0.8)


# --- load_scenarios --------------------------------------------------------


def test_load_scenarios_returns_validated_objects(tmp_path: Path) -> None:
    """Loader parses JSON and reifies each entry as a Scenario, with
    candidates validated as ``HolderCandidate`` instances."""
    path = _write_synthetic(tmp_path)
    scenarios = load_scenarios(path)
    assert len(scenarios) == 2
    assert all(isinstance(s, Scenario) for s in scenarios)
    assert all(isinstance(c, HolderCandidate) for s in scenarios for c in s.candidates)


def test_load_scenarios_rejects_bad_ranking_permutation(
    tmp_path: Path,
) -> None:
    """If ``expected_ranking`` doesn't cover the same symbol set as
    ``candidates``, validation fails immediately on load — better to
    crash than to silently produce a confusing eval result."""
    bad = [
        {
            "id": "bad-001",
            "description": "ranking missing a candidate",
            "candidates": [
                {"symbol": "MEM-A", "is_consortium_member": True},
                {"symbol": "MEM-B", "is_consortium_member": True},
            ],
            "expected_chosen": "MEM-A",
            "expected_ranking": ["MEM-A"],
            "notes": "bug fixture",
        }
    ]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="not a permutation"):
        load_scenarios(p)


def test_load_scenarios_rejects_bad_chosen(tmp_path: Path) -> None:
    """``expected_chosen`` must appear among candidate symbols."""
    bad = [
        {
            "id": "bad-002",
            "description": "chosen not in candidates",
            "candidates": [{"symbol": "MEM-A", "is_consortium_member": True}],
            "expected_chosen": "GHOST",
            "expected_ranking": ["MEM-A"],
            "notes": "bug fixture",
        }
    ]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="not in candidate symbols"):
        load_scenarios(p)


def test_load_scenarios_rejects_inconsistent_empty(tmp_path: Path) -> None:
    """Empty candidate list demands ``expected_chosen=None`` and an
    empty ranking — anything else is a fixture authoring mistake."""
    bad = [
        {
            "id": "bad-003",
            "description": "empty but expects a chosen",
            "candidates": [],
            "expected_chosen": "MEM-A",
            "expected_ranking": [],
            "notes": "bug fixture",
        }
    ]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="empty candidates"):
        load_scenarios(p)


# --- evaluate --------------------------------------------------------------


async def test_evaluate_aggregates_top1_and_spearman(tmp_path: Path) -> None:
    """End-to-end: run the rules baseline against the synthetic 2-set,
    confirm the aggregate matches what we expect from the closed-form
    spearman + binary top-1.

    synth-001: single candidate → match, Spearman=None (skipped)
    synth-002: rules picks MEM-A, ground truth = MEM-A → match,
               rho = 1.0
    Expected aggregate: top1 = 2/2 = 1.0; mean_spearman = mean([1.0])
    = 1.0 (one contributing scenario).
    """
    from agora.agents.routing import RoutingAgent

    path = _write_synthetic(tmp_path)
    scenarios = load_scenarios(path)
    report = await evaluate(RoutingAgent(), scenarios)

    assert isinstance(report, EvalReport)
    assert report.total == 2
    assert report.top1_accuracy == 1.0
    assert report.mean_spearman == 1.0
    assert all(isinstance(r, ScenarioResult) for r in report.results)
    # synth-001's spearman must be None (single candidate).
    synth_001 = next(r for r in report.results if r.scenario_id == "synth-001")
    assert synth_001.spearman is None
    # synth-002's spearman must be 1.0.
    synth_002 = next(r for r in report.results if r.scenario_id == "synth-002")
    assert synth_002.spearman == 1.0


async def test_evaluate_handles_empty_scenarios() -> None:
    """Empty scenario list → top-1 = 0.0, mean_spearman = None.

    Defensive: protects against a divide-by-zero if someone runs the
    harness against an empty fixture (e.g. mid-merge with a fixture
    file partially deleted)."""
    from agora.agents.routing import RoutingAgent

    report = await evaluate(RoutingAgent(), [])
    assert report.total == 0
    assert report.top1_accuracy == 0.0
    assert report.mean_spearman is None
    assert report.results == []


# --- CLI -------------------------------------------------------------------


def test_main_writes_baseline_to_given_path(tmp_path: Path) -> None:
    """``python -m agora.evals.routing --scenarios X --baseline Y``
    writes the report to the path given on the CLI, not the
    repo-root default. This is what the harness loader test would
    use if it ever invoked the CLI directly."""
    scenarios_path = _write_synthetic(tmp_path)
    baseline_path = tmp_path / "out.json"
    rc = main(
        [
            "--scenarios",
            str(scenarios_path),
            "--baseline",
            str(baseline_path),
        ]
    )
    assert rc == 0
    assert baseline_path.exists()
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert payload["total"] == 2
    assert payload["top1_accuracy"] == 1.0
    assert payload["mean_spearman"] == 1.0


def test_main_no_write_does_not_create_baseline(tmp_path: Path) -> None:
    """``--no-write`` is the dry-run mode for previewing scores
    without churning the committed baseline."""
    scenarios_path = _write_synthetic(tmp_path)
    baseline_path = tmp_path / "should-not-exist.json"
    rc = main(
        [
            "--scenarios",
            str(scenarios_path),
            "--baseline",
            str(baseline_path),
            "--no-write",
        ]
    )
    assert rc == 0
    assert not baseline_path.exists()


# ---------------------------------------------------------------------------
# Scenario.validate() — empty candidates happy path (line 129)
# ---------------------------------------------------------------------------


def test_validate_empty_candidates_no_expected_passes() -> None:
    """Empty candidates + None chosen + empty ranking is a valid no-holders
    scenario.  validate() must return without raising (line 129 early return)."""
    sc = Scenario(
        id="empty-ok",
        description="no holders found",
        candidates=[],
        expected_chosen=None,
        expected_ranking=[],
    )
    sc.validate()  # must not raise


def test_load_scenarios_empty_candidates_ok(tmp_path: Path) -> None:
    """load_scenarios accepts a scenario with zero candidates."""
    fixture: list[dict[str, object]] = [
        {
            "id": "empty-001",
            "description": "no holders",
            "candidates": [],
            "expected_chosen": None,
            "expected_ranking": [],
        }
    ]
    p = tmp_path / "empty.json"
    p.write_text(json.dumps(fixture), encoding="utf-8")
    scenarios = load_scenarios(p)
    assert len(scenarios) == 1
    assert scenarios[0].candidates == []


# ---------------------------------------------------------------------------
# _check_floor() — all three exit-code branches (lines 315-344)
# ---------------------------------------------------------------------------


def _write_baseline(tmp_path: Path, top1: float, rho: float | None) -> Path:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"top1_accuracy": top1, "mean_spearman": rho}),
        encoding="utf-8",
    )
    return baseline


def _make_report(top1: float = 1.0, rho: float | None = 1.0) -> EvalReport:
    return EvalReport(total=1, top1_accuracy=top1, mean_spearman=rho, results=[])


def test_check_floor_missing_baseline_returns_2(tmp_path: Path) -> None:
    code = _check_floor(_make_report(), tmp_path / "missing.json")
    assert code == 2


def test_check_floor_top1_below_floor_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = _write_baseline(tmp_path, top1=0.9, rho=0.8)
    code = _check_floor(_make_report(top1=0.5, rho=0.9), baseline)
    assert code == 1
    assert "FLOOR CHECK FAILED" in capsys.readouterr().out


def test_check_floor_spearman_below_floor_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = _write_baseline(tmp_path, top1=0.5, rho=0.9)
    code = _check_floor(_make_report(top1=1.0, rho=0.5), baseline)
    assert code == 1


def test_check_floor_above_floor_returns_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = _write_baseline(tmp_path, top1=0.5, rho=0.5)
    code = _check_floor(_make_report(top1=1.0, rho=1.0), baseline)
    assert code == 0
    assert "FLOOR CHECK OK" in capsys.readouterr().out


def test_check_floor_null_baseline_spearman_skips_rho(tmp_path: Path) -> None:
    baseline = _write_baseline(tmp_path, top1=0.5, rho=None)
    # rho in report is low but floor has no rho — should not fail
    code = _check_floor(_make_report(top1=1.0, rho=0.0), baseline)
    assert code == 0


# ---------------------------------------------------------------------------
# main() — --check-floor path (line 434)
# ---------------------------------------------------------------------------


def test_main_check_floor_pass(tmp_path: Path) -> None:
    scenarios_path = _write_synthetic(tmp_path)
    baseline_path = _write_baseline(tmp_path, top1=0.0, rho=0.0)
    rc = main(
        [
            "--rules-only",
            "--scenarios",
            str(scenarios_path),
            "--baseline",
            str(baseline_path),
            "--check-floor",
        ]
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# main() — --llm with get_llm_tiebreaker() returning None (lines 415-425)
# ---------------------------------------------------------------------------


def test_main_llm_no_tiebreaker_returns_2(tmp_path: Path) -> None:
    """When AGORA_ROUTING_LLM_ENABLED is not set, get_llm_tiebreaker()
    returns None; main() must surface this as exit code 2."""
    scenarios_path = _write_synthetic(tmp_path)
    with patch("agora.agents.factories.get_llm_tiebreaker", return_value=None):
        rc = main(["--llm", "--scenarios", str(scenarios_path), "--no-write"])
    assert rc == 2


def test_main_llm_with_tiebreaker_runs(tmp_path: Path) -> None:
    """--llm with non-None tiebreaker constructs RoutingAgent with it (line 425)."""
    from unittest.mock import AsyncMock, MagicMock

    from agora.evals.routing import EvalReport

    scenarios_path = _write_synthetic(tmp_path)
    fake_report = EvalReport(total=2, top1_accuracy=1.0, mean_spearman=1.0, results=[])
    with (
        patch(
            "agora.agents.factories.get_llm_tiebreaker",
            return_value=MagicMock(),
        ),
        patch(
            "agora.evals.routing.evaluate",
            new=AsyncMock(return_value=fake_report),
        ),
    ):
        rc = main(["--llm", "--scenarios", str(scenarios_path), "--no-write"])
    assert rc == 0
