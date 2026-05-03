"""RoutingAgent eval harness.

Runs a ``RoutingAgent`` (or any object exposing the same async
``run(candidates) -> RoutingRecommendation`` contract) against a set
of hand-labeled scenarios and scores it on two metrics:

- **Top-1 accuracy** — fraction of scenarios where the agent's
  ``chosen.symbol`` equals the scenario's ``expected_chosen``. Binary
  per scenario, mean across the set.
- **Mean Spearman rank correlation** — for each scenario where the
  agent ranks ≥2 candidates, compute Spearman rho between the actual and
  expected orderings of the symbols, then average across scenarios.
  Scenarios with <2 candidates contribute ``None`` and are skipped from
  the mean.

The harness is intentionally framework-agnostic: it imports only the
public ``RoutingAgent`` + ``HolderCandidate`` API. PR-2 will plug in
an LLM-augmented variant by passing a different agent instance — the
harness, scenarios, and baseline file stay unchanged.

Why these metrics, why now:

- Top-1 is the user-visible signal — staff sees one chosen supplier
  with one rationale. If we don't get pick #1 right, nothing else
  matters.
- Spearman captures the full ordering quality, which matters when
  staff overrides the chosen supplier and walks down the ranked list.
  It's tie-tolerant (ranks not raw scores) and cheap to compute.
- NDCG was considered and rejected — overkill for a 20-scenario
  prototype set, and graded relevance labels would require a second
  labeler pass.

See ADR-0014 for the full rationale and the gating policy (PR-2 must
beat both numbers committed in the baseline).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agora.agents.routing import RoutingAgent, RoutingRecommendation
from agora.models.candidate import HolderCandidate

# Repo-root-relative defaults. The harness can be invoked with explicit
# paths (e.g. from ``tests/test_eval_harness.py`` against a synthetic
# fixture) — these only kick in when CLI args are absent.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCENARIOS = _REPO_ROOT / "evals" / "routing" / "scenarios.json"
DEFAULT_BASELINE = _REPO_ROOT / "evals" / "routing" / "baseline.json"


class _RoutingAgentLike(Protocol):
    """Minimal protocol so PR-2 can pass a wrapped LLM agent.

    Matches ``RoutingAgent.run`` exactly — async, candidates in,
    ``RoutingRecommendation`` out.
    """

    async def run(self, candidates: list[HolderCandidate]) -> RoutingRecommendation: ...


# --- Scenario / report dataclasses ----------------------------------------


@dataclass(slots=True)
class Scenario:
    """One labeled routing situation.

    ``expected_ranking`` lists symbols in best-first order. It MUST be
    a permutation of the candidate symbols; the harness fails loudly
    on mismatch rather than silently scoring against a malformed
    fixture.
    """

    id: str
    description: str
    candidates: list[HolderCandidate]
    expected_chosen: str | None
    expected_ranking: list[str]
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scenario:
        cands = [HolderCandidate.model_validate(c) for c in data["candidates"]]
        return cls(
            id=str(data["id"]),
            description=str(data.get("description", "")),
            candidates=cands,
            expected_chosen=data.get("expected_chosen"),
            expected_ranking=list(data.get("expected_ranking", [])),
            notes=str(data.get("notes", "")),
        )

    def validate(self) -> None:
        """Catch fixture authoring mistakes early.

        - ``expected_ranking`` must be a permutation of candidate
          symbols (no extras, no missing).
        - ``expected_chosen``, if set, must appear in candidate symbols.
        - Empty-candidate scenarios are allowed (rules baseline path
          for the no-holders case) and require ``expected_chosen=None``
          + empty ranking.
        """
        symbols = [c.symbol for c in self.candidates]
        if not symbols:
            if self.expected_chosen is not None or self.expected_ranking:
                raise ValueError(
                    f"scenario {self.id}: empty candidates must have "
                    "expected_chosen=None and empty expected_ranking"
                )
            return
        if sorted(self.expected_ranking) != sorted(symbols):
            raise ValueError(
                f"scenario {self.id}: expected_ranking {self.expected_ranking} "
                f"is not a permutation of candidate symbols {symbols}"
            )
        if self.expected_chosen is not None and self.expected_chosen not in symbols:
            raise ValueError(
                f"scenario {self.id}: expected_chosen "
                f"{self.expected_chosen!r} not in candidate symbols {symbols}"
            )


@dataclass(slots=True)
class ScenarioResult:
    scenario_id: str
    chosen_match: bool
    actual_chosen: str | None
    expected_chosen: str | None
    actual_ranking: list[str]
    expected_ranking: list[str]
    spearman: float | None  # None when <2 candidates


@dataclass(slots=True)
class EvalReport:
    """Aggregate scores across the scenario set.

    Stored on disk as ``baseline.json`` for the rules-baseline run; PR-2
    diffs against this snapshot. Floats rounded to 4 dp on serialize so
    cosmetic float drift doesn't churn the diff.
    """

    total: int
    top1_accuracy: float
    mean_spearman: float | None  # None if no scenario contributed a Spearman
    results: list[ScenarioResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "top1_accuracy": round(self.top1_accuracy, 4),
            "mean_spearman": (None if self.mean_spearman is None else round(self.mean_spearman, 4)),
            "results": [
                {
                    **asdict(r),
                    "spearman": (None if r.spearman is None else round(r.spearman, 4)),
                }
                for r in self.results
            ],
        }


# --- Public API ------------------------------------------------------------


def load_scenarios(path: Path) -> list[Scenario]:
    """Read + validate a scenarios JSON file.

    The file is a JSON array of scenario objects. Each scenario is
    validated immediately on load so a fixture authoring mistake fails
    here rather than producing a confusing eval result downstream.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    scenarios = [Scenario.from_dict(item) for item in raw]
    for s in scenarios:
        s.validate()
    return scenarios


def spearman(actual: list[str], expected: list[str]) -> float | None:
    """Spearman rank correlation between two orderings of the same set.

    Returns ``None`` when n < 2 (correlation undefined for a single
    element) or when the two lists don't cover the same symbol set
    (caller bug — surfaced as None rather than silently zero so the
    aggregate doesn't get poisoned). With no ties (which is the case
    here — both rankings are total orders over distinct symbols) the
    standard formula applies::

        rho = 1 - (6 · Σ d²) / (n · (n² - 1))

    where ``d`` is the rank difference for each element. Range
    ``[-1, 1]``; 1 = identical, 0 = no correlation, -1 = reversed.
    """
    if len(actual) != len(expected):
        return None
    n = len(actual)
    if n < 2:
        return None
    if set(actual) != set(expected):
        return None
    rank_actual = {s: i for i, s in enumerate(actual)}
    rank_expected = {s: i for i, s in enumerate(expected)}
    sum_d2 = sum((rank_actual[s] - rank_expected[s]) ** 2 for s in rank_actual)
    return 1.0 - (6.0 * sum_d2) / (n * (n * n - 1))


async def evaluate(agent: _RoutingAgentLike, scenarios: list[Scenario]) -> EvalReport:
    """Run ``agent`` against every scenario, return an aggregate report."""
    results: list[ScenarioResult] = []
    spearman_values: list[float] = []
    matches = 0

    for sc in scenarios:
        rec = await agent.run(sc.candidates)
        actual_chosen = rec.chosen.symbol if rec.chosen is not None else None
        actual_ranking = [c.symbol for c in rec.ranked]
        match = actual_chosen == sc.expected_chosen
        if match:
            matches += 1
        rho = spearman(actual_ranking, sc.expected_ranking)
        if rho is not None:
            spearman_values.append(rho)
        results.append(
            ScenarioResult(
                scenario_id=sc.id,
                chosen_match=match,
                actual_chosen=actual_chosen,
                expected_chosen=sc.expected_chosen,
                actual_ranking=actual_ranking,
                expected_ranking=sc.expected_ranking,
                spearman=rho,
            )
        )

    total = len(scenarios)
    top1 = matches / total if total else 0.0
    mean_rho = sum(spearman_values) / len(spearman_values) if spearman_values else None
    return EvalReport(
        total=total,
        top1_accuracy=top1,
        mean_spearman=mean_rho,
        results=results,
    )


def write_baseline(report: EvalReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline matches the rest of the repo's JSON conventions
    # (e.g. detect-secrets baseline) and avoids a noisy POSIX-tooling
    # warning.
    path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --- CLI -------------------------------------------------------------------


def _format_summary(report: EvalReport) -> str:
    lines = [
        f"scenarios:        {report.total}",
        f"top-1 accuracy:   {report.top1_accuracy:.4f}",
        (
            "mean Spearman:    n/a"
            if report.mean_spearman is None
            else f"mean Spearman:    {report.mean_spearman:.4f}"
        ),
        "",
        "per-scenario:",
    ]
    for r in report.results:
        rho = "n/a" if r.spearman is None else f"{r.spearman:+.3f}"
        # ASCII-only flags so the summary prints on Windows cp1252 without
        # forcing PYTHONIOENCODING tweaks.
        flag = "OK" if r.chosen_match else "--"
        lines.append(
            f"  {flag} {r.scenario_id:<16} "
            f"chose={r.actual_chosen!s:<8} expect={r.expected_chosen!s:<8} "
            f"rho={rho}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """``python -m agora.evals.routing`` entrypoint.

    Runs the rules-baseline ``RoutingAgent`` against the committed
    scenarios and writes ``evals/routing/baseline.json``. PR-2 will add
    a ``--agent`` flag to swap in the LLM variant, but the rules
    baseline is intentionally the only mode in PR-1 — that's what we
    commit as the floor.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Score the rules-baseline RoutingAgent against the "
        "committed scenario set and write a baseline report."
    )
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=DEFAULT_SCENARIOS,
        help=f"path to scenarios JSON (default: {DEFAULT_SCENARIOS})",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=(
            "path to write baseline JSON "
            f"(default: {DEFAULT_BASELINE}); pass /dev/null or omit "
            "with --no-write to print only"
        ),
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="print summary only; do not write baseline.json",
    )
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios)
    agent = RoutingAgent()
    report = asyncio.run(evaluate(agent, scenarios))
    print(_format_summary(report))
    if not args.no_write:
        write_baseline(report, args.baseline)
        print(f"\nwrote {args.baseline}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI shim
    raise SystemExit(main())
