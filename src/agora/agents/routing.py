"""RoutingAgent — rank holders against consortium policy.

The prototype uses a deterministic scoring function (no LLM) so that
ranking is repeatable and testable. Future versions can add an LLM
tie-breaker for ambiguous cases.

Score components, all 0..1, weighted sum:
- consortium membership (weight 0.5)
- preferred_score from discovery (weight 0.2)
- holding status: available > unknown > on_loan (weight 0.2)
- proximity, lower km better (weight 0.1, optional)
"""

from __future__ import annotations

from dataclasses import dataclass

from agora.models.candidate import HolderCandidate


@dataclass(slots=True)
class RoutingRecommendation:
    ranked: list[HolderCandidate]
    chosen: HolderCandidate | None
    rationale: str


_STATUS_SCORE: dict[str, float] = {
    "available": 1.0,
    "unknown": 0.5,
    "on_loan": 0.2,
    "reference_only": 0.0,
}


class RoutingAgent:
    """Rank candidate holders to pick a primary supplier."""

    def __init__(self, *, max_distance_km: float = 1500.0):
        self._max_distance = max_distance_km

    async def run(
        self, candidates: list[HolderCandidate]
    ) -> RoutingRecommendation:
        if not candidates:
            return RoutingRecommendation(
                ranked=[], chosen=None, rationale="no candidates to rank"
            )

        scored = sorted(
            candidates,
            key=lambda c: self._score(c),
            reverse=True,
        )
        chosen = scored[0]
        rationale = self._make_rationale(chosen, len(scored))
        return RoutingRecommendation(
            ranked=scored, chosen=chosen, rationale=rationale
        )

    def _score(self, c: HolderCandidate) -> float:
        consortium = 1.0 if c.is_consortium_member else 0.0
        preferred = c.preferred_score
        status = _STATUS_SCORE.get(c.status, 0.5)
        if c.distance_km is not None:
            proximity = max(0.0, 1.0 - (c.distance_km / self._max_distance))
        else:
            proximity = 0.5
        return (
            0.5 * consortium
            + 0.2 * preferred
            + 0.2 * status
            + 0.1 * proximity
        )

    def _make_rationale(self, chosen: HolderCandidate, total: int) -> str:
        tier = "consortium member" if chosen.is_consortium_member else "external"
        return (
            f"Chosen {chosen.symbol} ({tier}, status={chosen.status}) "
            f"from {total} candidate(s) by weighted score."
        )
