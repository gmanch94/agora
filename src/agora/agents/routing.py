"""RoutingAgent — rank holders against consortium policy.

The prototype uses a deterministic weighted-sum scoring function
(repeatable, testable, offline-runnable) and optionally consults an
``LlmTiebreaker`` to resolve near-ties — i.e. cases where the rules
produce two top candidates whose scores differ by less than ε. The
LLM never replaces the rules; it only fires on near-tie. See
**ADR-0014** for the gating policy and the rules-baseline floor.

Score components, all 0..1, weighted sum:

- consortium membership (weight 0.5)
- preferred_score from discovery (weight 0.2)
- holding status: available > unknown > on_loan > reference_only (weight 0.2)
- proximity, lower km better (weight 0.1; defaults to 0.5 when distance is unknown)

PR-2a (this file's seam) ships only the integration point + a
``MockLlmTiebreaker`` for tests. PR-2b will ship the real ADK-mediated
adapter + the prompt template + an updated ``baseline.json``.

**Invariants** (pin against PRD-02 + ADR-0005 + ADR-0014):

- ``RoutingAgent.run`` is async, candidates in, ``RoutingRecommendation``
  out. The new optional ``item`` kwarg is backward-compatible
  (callers that pass only candidates keep working — proven by the
  unchanged ``test_routing_picks_consortium_available_first`` regression
  test in ``tests/test_agents.py``).
- ``rationale`` ≤3 sentences regardless of which path produced it.
- Advisory-only — never writes to the saga ledger.
- Empty ``candidates`` → ``chosen=None`` + empty ranking + no LLM call.
- LLM-tiebreaker failures (raise / abstain / unknown symbol) **always**
  fall back to the rules pick + a diagnostic in the rationale. The
  agent never raises out to its caller because of the LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from agora.config import get_settings
from agora.logging import get_logger
from agora.models.candidate import HolderCandidate
from agora.models.request import ItemMetadata

log = get_logger(__name__)


# Module-level alias for the test-double responder. Defined before
# ``MockLlmTiebreaker`` so the constructor signature can reference it
# without a forward-reference string. ``TypeAlias`` makes the intent
# explicit to mypy + future readers.
ResponderFn: TypeAlias = Callable[[list[HolderCandidate]], "TiebreakDecision"]


@dataclass(slots=True)
class RoutingRecommendation:
    ranked: list[HolderCandidate]
    chosen: HolderCandidate | None
    rationale: str


@dataclass(slots=True)
class TiebreakDecision:
    """Result of an ``LlmTiebreaker.resolve`` call.

    ``chosen_symbol`` is either an ISIL/consortium-local symbol that
    appears in the input candidate list, or ``None`` to signal
    "abstain — keep the rules pick." The agent treats an unknown
    symbol the same as ``None``: rules pick wins, diagnostic logged.

    ``rationale`` is the LLM's free-text justification, expected to be
    one short sentence. ``RoutingAgent`` composes its own meta-prefix
    around it; the combined rationale must stay ≤3 sentences (PRD-02).
    PR-2b's prompt template is responsible for keeping the LLM rationale
    short enough.
    """

    chosen_symbol: str | None
    rationale: str


class LlmTiebreaker(Protocol):
    """Pluggable tie-breaker contract.

    ``resolve`` receives the candidates already sorted by rules score
    (best first; the agent guarantees ≥2 entries when calling) and
    optionally the request item so the adapter can read patron-side
    metadata (request format, year, etc.) that lives on
    ``ItemMetadata`` and not on individual candidates.

    Implementations MUST NOT raise — but if they do, the agent catches
    and falls back. Returning ``TiebreakDecision(chosen_symbol=None,
    rationale="...")`` is the explicit-abstain path.
    """

    async def resolve(
        self,
        candidates: list[HolderCandidate],
        *,
        item: ItemMetadata | None = None,
    ) -> TiebreakDecision: ...


_STATUS_SCORE: dict[str, float] = {
    "available": 1.0,
    "unknown": 0.5,
    "on_loan": 0.2,
    "reference_only": 0.0,
}


class RoutingAgent:
    """Rank candidate holders to pick a primary supplier.

    Construct with no args for the rules-only baseline path (the
    backward-compatible default). Pass ``llm_tiebreaker=`` to enable
    LLM-augmented tie-breaking; pass ``epsilon=`` to override the
    Settings-driven default (most callers should leave ``epsilon=None``
    and tune via ``AGORA_ROUTING_TIEBREAK_EPSILON``).
    """

    def __init__(
        self,
        *,
        max_distance_km: float = 1500.0,
        llm_tiebreaker: LlmTiebreaker | None = None,
        epsilon: float | None = None,
    ):
        self._max_distance = max_distance_km
        self._llm = llm_tiebreaker
        # Resolve ε at construction time. Reading Settings here (rather
        # than every ``run``) means a test can monkeypatch the env once
        # and instantiate the agent — but it also means env changes
        # post-construction are NOT picked up. That's the right
        # trade-off for a dependency injected via constructor; tests
        # that care about ε flips just construct two agents.
        if epsilon is not None:
            self._epsilon = epsilon
        else:
            self._epsilon = get_settings().routing_tiebreak_epsilon

    async def run(
        self,
        candidates: list[HolderCandidate],
        *,
        item: ItemMetadata | None = None,
    ) -> RoutingRecommendation:
        if not candidates:
            return RoutingRecommendation(ranked=[], chosen=None, rationale="no candidates to rank")

        scored = sorted(candidates, key=lambda c: self._score(c, item), reverse=True)
        rules_chosen = scored[0]

        # Rules-only path: no LLM, single candidate, or top-2 gap exceeds ε.
        # Each branch produces the same ``RoutingRecommendation`` shape so
        # downstream consumers (eval harness, tests, future API endpoint)
        # don't have to know whether the LLM fired.
        if self._llm is None or len(scored) < 2:
            return RoutingRecommendation(
                ranked=scored,
                chosen=rules_chosen,
                rationale=self._make_rationale(rules_chosen, len(scored)),
            )

        gap = self._score(scored[0], item) - self._score(scored[1], item)
        if gap > self._epsilon:
            return RoutingRecommendation(
                ranked=scored,
                chosen=rules_chosen,
                rationale=self._make_rationale(rules_chosen, len(scored)),
            )

        # Within ε — call the LLM tie-breaker. Any failure path returns
        # the rules pick with a composed diagnostic; we never re-raise.
        return await self._call_tiebreaker(scored, rules_chosen, gap, item)

    async def _call_tiebreaker(
        self,
        scored: list[HolderCandidate],
        rules_chosen: HolderCandidate,
        gap: float,
        item: ItemMetadata | None,
    ) -> RoutingRecommendation:
        # Type narrowing: this method is only called from the post-len-check
        # branch, so the LLM is configured. ``assert`` is for mypy, not
        # runtime safety — defensive ``return`` follows in case the
        # invariant ever drifts.
        assert self._llm is not None  # nosec B101  # mypy narrowing
        try:
            decision = await self._llm.resolve(scored, item=item)
        except Exception as exc:  # defensive — LLMs can fail in any way
            log.warning(
                "routing.tiebreaker.failed",
                error=str(exc),
                rules_chosen=rules_chosen.symbol,
            )
            return RoutingRecommendation(
                ranked=scored,
                chosen=rules_chosen,
                rationale=(
                    f"Rules tied {scored[0].symbol}/{scored[1].symbol} "
                    f"at gap {gap:.3f}; LLM tie-breaker unavailable, "
                    f"kept {rules_chosen.symbol}."
                ),
            )

        if decision.chosen_symbol is None:
            log.info(
                "routing.tiebreaker.abstained",
                rules_chosen=rules_chosen.symbol,
            )
            return RoutingRecommendation(
                ranked=scored,
                chosen=rules_chosen,
                rationale=(
                    f"Rules tied {scored[0].symbol}/{scored[1].symbol} "
                    f"at gap {gap:.3f}; LLM tie-breaker abstained, "
                    f"kept {rules_chosen.symbol}."
                ),
            )

        by_symbol = {c.symbol: c for c in scored}
        llm_chosen = by_symbol.get(decision.chosen_symbol)
        if llm_chosen is None:
            # The LLM hallucinated a symbol that isn't in the candidate
            # list. Treat as a malformed response: rules pick wins, log
            # so PR-2b can tighten the prompt if this happens often.
            log.warning(
                "routing.tiebreaker.invalid_symbol",
                returned=decision.chosen_symbol,
                rules_chosen=rules_chosen.symbol,
            )
            return RoutingRecommendation(
                ranked=scored,
                chosen=rules_chosen,
                rationale=(
                    f"Rules tied {scored[0].symbol}/{scored[1].symbol} "
                    f"at gap {gap:.3f}; LLM tie-breaker returned unknown "
                    f"symbol, kept {rules_chosen.symbol}."
                ),
            )

        # Reorder so the LLM-chosen candidate appears first; preserve
        # the rules-baseline order for the rest. Staff overriding pick
        # #1 walks the remaining list, which still reflects the rules
        # ordering they understand.
        new_ranked = [llm_chosen] + [c for c in scored if c.symbol != llm_chosen.symbol]
        return RoutingRecommendation(
            ranked=new_ranked,
            chosen=llm_chosen,
            rationale=(
                f"Rules tied {scored[0].symbol}/{scored[1].symbol} at gap "
                f"{gap:.3f}; LLM picked {llm_chosen.symbol}: "
                f"{decision.rationale}"
            ),
        )

    def _score(self, c: HolderCandidate, item: ItemMetadata | None = None) -> float:
        consortium = 1.0 if c.is_consortium_member else 0.0
        preferred = c.preferred_score
        status = _STATUS_SCORE.get(c.status, 0.5)
        if c.distance_km is not None:
            proximity = max(0.0, 1.0 - (c.distance_km / self._max_distance))
        else:
            proximity = 0.5
        base = 0.5 * consortium + 0.2 * preferred + 0.2 * status + 0.1 * proximity
        # Format-affinity adjustment: for article-shaped requests, the
        # candidate's delivery channel is load-bearing — digital fulfilment
        # is essentially required, physical-only is borderline disqualifying.
        # Symmetric +/- 0.3 swing is large enough to overcome the 0.5
        # consortium weight when the consortium candidate is physical-only
        # and an external candidate offers electronic delivery (the
        # `routing-015` shape). For book/other requests this term is zero
        # and rules behaviour is unchanged. See ADR-0014 addendum on
        # "single-axis tractable signal → add feature; multi-signal cross-
        # feature reasoning → lean LLM."
        return base + self._format_affinity(c, item)

    @staticmethod
    def _format_affinity(c: HolderCandidate, item: ItemMetadata | None) -> float:
        if item is None or item.item_kind not in {"article", "chapter"}:
            return 0.0
        delivery = (c.raw or {}).get("delivery") if c.raw else None
        if delivery == "electronic":
            return 0.3
        if delivery == "physical_only":
            return -0.3
        return 0.0

    def _make_rationale(self, chosen: HolderCandidate, total: int) -> str:
        tier = "consortium member" if chosen.is_consortium_member else "external"
        return (
            f"Chosen {chosen.symbol} ({tier}, status={chosen.status}) "
            f"from {total} candidate(s) by weighted score."
        )


# --- Test double ----------------------------------------------------------


class MockLlmTiebreaker:
    """Deterministic ``LlmTiebreaker`` for tests.

    Configured with a callable that receives the candidate list and
    returns a ``TiebreakDecision`` (or raises). This lets tests pin
    "LLM picks X", "LLM abstains", "LLM raises", and "LLM returns
    bogus symbol" without any prompt or network. PR-2b will not use
    this — it ships a real ADK-backed adapter — but the Protocol
    contract is the same so the test surface stays valid.

    ``call_count`` is a simple instrumentation field; tests use it to
    assert the agent did NOT call the LLM when the gap exceeded ε.
    """

    def __init__(
        self,
        responder: ResponderFn,
    ) -> None:
        self._responder = responder
        self.call_count = 0
        self.last_candidates: list[HolderCandidate] | None = None
        self.last_item: ItemMetadata | None = None

    async def resolve(
        self,
        candidates: list[HolderCandidate],
        *,
        item: ItemMetadata | None = None,
    ) -> TiebreakDecision:
        self.call_count += 1
        self.last_candidates = list(candidates)
        self.last_item = item
        return self._responder(candidates)
