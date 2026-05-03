"""RoutingAgent LLM tie-breaker integration tests.

Pins the seam between the rules-baseline scoring and the optional
``LlmTiebreaker``. The advisor's six-case matrix maps to the test
functions below; together they cover every branch in
``RoutingAgent._call_tiebreaker`` plus the gap-vs-ε guard in
``RoutingAgent.run``.

Existing rules-only behaviour is regression-pinned by
``tests/test_agents.py::test_routing_picks_consortium_available_first``
— intentionally not duplicated here so a future refactor only has to
update one happy-path assertion.

Why ``MockLlmTiebreaker`` lives in ``agora.agents.routing`` and not in
``tests/`` (or under ``conftest.py``):

- It satisfies the public ``LlmTiebreaker`` protocol; making it a
  shipped test-double mirrors how ``MockReShareClient`` /
  ``MockCrossrefClient`` / ``MockSruClient`` ship from their respective
  client modules.
- PR-2b's prompt-development workflow will plausibly want the same
  mock to drive offline replay of recorded LLM responses; keeping it
  in-package keeps that path open.

Tests deliberately do NOT exercise ``evals/routing/scenarios.json``
or ``evals/routing/baseline.json`` — those are eval artifacts; PR-2b
is the PR that re-runs the harness against a real LLM.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from agora.agents.routing import (
    LlmTiebreaker,
    MockLlmTiebreaker,
    RoutingAgent,
    TiebreakDecision,
)
from agora.config import get_settings
from agora.models.candidate import HolderCandidate

# --- Shared fixtures -------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """The ``get_settings()`` cache is read at ``RoutingAgent.__init__``
    time, so any test that monkeypatches ``AGORA_ROUTING_TIEBREAK_EPSILON``
    (or any other Agora env var) needs the cache cleared on BOTH sides
    of the test — pytest's ``monkeypatch`` rewinds env vars but not the
    cached ``Settings()`` instance. The clear-before / yield /
    clear-after pattern matches ``tests/test_factories.py``.
    """
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _tied_pair() -> list[HolderCandidate]:
    """Two consortium candidates with rules score gap = 0.0.

    Mirrors the design of the four ground-truth-vs-rules disagreement
    scenarios in the eval set (013, 014, 016 specifically); rules
    rank by stable sort because the score is identical, so the
    LLM is the only signal that can tell them apart.
    """
    return [
        HolderCandidate(
            symbol="MEM-A",
            is_consortium_member=True,
            status="available",
            preferred_score=0.5,
            raw={"sla_tier": "B"},
        ),
        HolderCandidate(
            symbol="MEM-B",
            is_consortium_member=True,
            status="available",
            preferred_score=0.5,
            raw={"sla_tier": "A"},
        ),
    ]


def _wide_gap_pair() -> list[HolderCandidate]:
    """One consortium-available holder vs one external; gap ≈ 0.46.

    Rules pick MEM-A by a wide margin. With ε well below 0.46 the
    LLM tie-breaker MUST NOT fire (cost protection — wide-gap cases
    are rules-confident and shouldn't burn an LLM call)."""
    return [
        HolderCandidate(
            symbol="MEM-A",
            is_consortium_member=True,
            status="available",
            preferred_score=0.5,
        ),
        HolderCandidate(
            symbol="EXT",
            is_consortium_member=False,
            status="available",
            preferred_score=0.5,
        ),
    ]


# --- Test 1: backward-compatibility regression pin -------------------------


async def test_no_llm_tiebreaker_yields_unchanged_rules_path() -> None:
    """``RoutingAgent()`` with no llm kwarg behaves byte-identically to
    pre-PR-2a code. The eval harness, the API endpoint, every
    existing caller — all of them must keep working unchanged.

    The smoke check from ``tests/test_agents.py`` is the canonical
    regression pin; this test adds a redundant guard against the
    specific bug "ε read at init time accidentally short-circuits
    the rules-only path."
    """
    candidates = _tied_pair()
    agent = RoutingAgent()  # no llm_tiebreaker kwarg
    rec = await agent.run(candidates)

    assert rec.chosen is not None
    # Stable sort preserves input order for tied scores; MEM-A appears
    # first in `_tied_pair` so it's the rules pick.
    assert rec.chosen.symbol == "MEM-A"
    assert rec.ranked == candidates
    assert "Chosen MEM-A" in rec.rationale
    assert "LLM" not in rec.rationale  # no diagnostic when no LLM


# --- Test 2: gap above ε → tie-breaker NOT called -------------------------


async def test_wide_gap_does_not_call_tiebreaker() -> None:
    """When rules are confident (top-2 gap > ε), the LLM is not asked.

    This is the cost-protection invariant: every saga whose top-2 are
    not nearly tied burns zero LLM calls. ``MockLlmTiebreaker.call_count``
    is the assertion surface."""
    mock = MockLlmTiebreaker(
        responder=lambda _: TiebreakDecision(
            chosen_symbol="MEM-A", rationale="should not be called"
        )
    )
    agent = RoutingAgent(llm_tiebreaker=mock, epsilon=0.05)
    rec = await agent.run(_wide_gap_pair())

    assert mock.call_count == 0  # the load-bearing assertion
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-A"
    assert "LLM" not in rec.rationale


# --- Test 3: gap within ε → tie-breaker called, ranking reordered ---------


async def test_within_epsilon_calls_tiebreaker_and_reorders() -> None:
    """When rules tie within ε, the LLM picks; agent reorders the
    ``ranked`` list so the LLM-chosen candidate is first.

    The rationale composes the rules-tie context + the LLM's reason
    in a single sentence so it stays inside the PRD-02 ≤3-sentence
    contract."""
    mock = MockLlmTiebreaker(
        responder=lambda _: TiebreakDecision(
            chosen_symbol="MEM-B",
            rationale="MEM-B has SLA tier A which beats MEM-A's tier B.",
        )
    )
    agent = RoutingAgent(llm_tiebreaker=mock, epsilon=0.05)
    candidates = _tied_pair()
    rec = await agent.run(candidates)

    assert mock.call_count == 1
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-B"
    # Ranking reordered: LLM pick first, others in rules order behind it.
    assert [c.symbol for c in rec.ranked] == ["MEM-B", "MEM-A"]
    # Rationale stitches the meta-prefix (rules tied X/Y at gap Z; LLM
    # picked Y) onto the LLM's own reason.
    assert "MEM-A/MEM-B" in rec.rationale
    assert "LLM picked MEM-B" in rec.rationale
    assert "SLA tier A" in rec.rationale


async def test_within_epsilon_passes_item_through_to_tiebreaker() -> None:
    """If a caller passes ``item=`` into ``RoutingAgent.run``, the
    LLM tie-breaker receives it. PR-2b's adapter reads
    ``ItemMetadata.item_kind`` etc. when composing the prompt; the
    seam already plumbs that through."""
    from agora.models.request import ItemMetadata

    mock = MockLlmTiebreaker(
        responder=lambda _: TiebreakDecision(chosen_symbol="MEM-A", rationale="rationale")
    )
    agent = RoutingAgent(llm_tiebreaker=mock, epsilon=0.05)
    item = ItemMetadata(title="Test", item_kind="article")
    await agent.run(_tied_pair(), item=item)

    assert mock.last_item is item


# --- Test 4: tie-breaker raises → fallback to rules + diagnostic ----------


async def test_tiebreaker_exception_falls_back_to_rules() -> None:
    """LLM call raises (network down, quota exceeded, malformed
    response, anything). Agent catches, returns the rules pick, logs
    a diagnostic in the rationale. **Never re-raises** — discovery
    /routing must always produce a recommendation for staff (advisory
    contract, ADR-0005)."""

    def boom(_: list[HolderCandidate]) -> TiebreakDecision:
        raise RuntimeError("LLM endpoint unreachable")

    mock = MockLlmTiebreaker(responder=boom)
    agent = RoutingAgent(llm_tiebreaker=mock, epsilon=0.05)
    rec = await agent.run(_tied_pair())

    assert mock.call_count == 1  # the LLM was attempted
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-A"  # rules fallback
    assert "LLM tie-breaker unavailable" in rec.rationale
    assert "kept MEM-A" in rec.rationale


# --- Test 5: tie-breaker returns symbol not in candidate set --------------


async def test_tiebreaker_unknown_symbol_falls_back_to_rules() -> None:
    """LLM hallucinates a symbol not in the input list. Agent treats
    as malformed: rules pick wins, log warning so PR-2b can tighten
    the prompt if hallucination shows up in production."""
    mock = MockLlmTiebreaker(
        responder=lambda _: TiebreakDecision(
            chosen_symbol="MEM-NONEXISTENT", rationale="hallucinated"
        )
    )
    agent = RoutingAgent(llm_tiebreaker=mock, epsilon=0.05)
    rec = await agent.run(_tied_pair())

    assert mock.call_count == 1
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-A"
    assert "unknown symbol" in rec.rationale
    assert "kept MEM-A" in rec.rationale


# --- Test 6: tie-breaker abstains -----------------------------------------


async def test_tiebreaker_abstain_falls_back_to_rules() -> None:
    """Explicit abstain path: ``chosen_symbol=None``. Distinct from the
    raise path so PR-2b can wire "low-confidence response" → abstain
    (vs "broken response" → exception). Agent surfaces the abstain
    in the rationale so staff can see the LLM was consulted but
    chose not to override."""
    mock = MockLlmTiebreaker(
        responder=lambda _: TiebreakDecision(
            chosen_symbol=None,
            rationale="not enough signal to differentiate",
        )
    )
    agent = RoutingAgent(llm_tiebreaker=mock, epsilon=0.05)
    rec = await agent.run(_tied_pair())

    assert mock.call_count == 1
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-A"
    assert "LLM tie-breaker abstained" in rec.rationale


# --- Edge cases tied to ε / Settings --------------------------------------


async def test_epsilon_from_settings_when_constructor_omits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``epsilon=None`` (the constructor default) must read
    ``Settings.routing_tiebreak_epsilon``. We flip the env var and
    confirm the agent picks up the override.

    This pins the env-var → behaviour wiring so PR-2b can tune ε via
    deployment config without a code change."""
    monkeypatch.setenv("AGORA_ROUTING_TIEBREAK_EPSILON", "0.5")
    get_settings.cache_clear()

    mock = MockLlmTiebreaker(
        responder=lambda _: TiebreakDecision(chosen_symbol="EXT", rationale="LLM override")
    )
    # No epsilon kwarg -> reads Settings -> 0.5. Wide-gap pair has
    # gap = 0.46, so 0.5 > 0.46 means the LLM SHOULD fire — exactly
    # what flipping the env var was meant to enable.
    agent = RoutingAgent(llm_tiebreaker=mock)
    rec = await agent.run(_wide_gap_pair())

    assert mock.call_count == 1
    assert rec.chosen is not None
    assert rec.chosen.symbol == "EXT"


async def test_single_candidate_short_circuits_no_llm_call() -> None:
    """One candidate → no second-place to tie against; LLM is not
    called regardless of ε. Empty-or-singleton inputs are the
    "rules trivially correct" path."""
    mock = MockLlmTiebreaker(
        responder=lambda _: TiebreakDecision(chosen_symbol="X", rationale="should not run")
    )
    agent = RoutingAgent(llm_tiebreaker=mock, epsilon=0.05)
    only = [
        HolderCandidate(
            symbol="MEM-A",
            is_consortium_member=True,
            status="available",
            preferred_score=0.5,
        )
    ]
    rec = await agent.run(only)

    assert mock.call_count == 0
    assert rec.chosen is not None
    assert rec.chosen.symbol == "MEM-A"


async def test_empty_candidates_short_circuits_no_llm_call() -> None:
    """Empty list → ``chosen=None`` + empty ranking + "no candidates"
    rationale. Consistent with pre-PR-2a behaviour and with the
    DiscoveryAgent's "zero holders matched" downstream signal."""
    mock = MockLlmTiebreaker(
        responder=lambda _: TiebreakDecision(chosen_symbol="X", rationale="should not run")
    )
    agent = RoutingAgent(llm_tiebreaker=mock, epsilon=0.05)
    rec = await agent.run([])

    assert mock.call_count == 0
    assert rec.chosen is None
    assert rec.ranked == []
    assert "no candidates" in rec.rationale


# --- Protocol surface check (compile-time, not runtime) -------------------


def test_mock_satisfies_protocol() -> None:
    """``MockLlmTiebreaker`` must structurally satisfy the
    ``LlmTiebreaker`` protocol — otherwise mypy catches the seam
    breaking when PR-2b adds a real adapter alongside.

    Runtime ``isinstance`` works only when the Protocol is
    ``@runtime_checkable``; we instead rely on the type-checker.
    A bare assignment surfaces the Liskov breakage at mypy time
    while keeping the test cheap at pytest time.
    """
    mock = MockLlmTiebreaker(responder=lambda _: TiebreakDecision(chosen_symbol=None, rationale=""))
    accept_protocol: LlmTiebreaker = mock  # mypy will reject if shape drifts
    assert accept_protocol is mock
