"""DiscoveryAgent — resolve a citation to candidate holders.

This is a deterministic rules-driven agent in the prototype. It calls
the SRU client and converts results into ``HolderCandidate`` records
that RoutingAgent consumes. No LLM call here — discovery is a
search-and-merge problem, not a judgement problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agora.clients.sru import SruClient, SruRecord
from agora.models.candidate import HolderCandidate
from agora.models.request import IllRequest


@dataclass(slots=True)
class DiscoveryRecommendation:
    candidates: list[HolderCandidate]
    diagnostics: list[str] = field(default_factory=list)
    rationale: str = ""

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)


class DiscoveryAgent:
    """Search SRU + (later) WorldCat for holders of the requested item."""

    def __init__(
        self,
        sru: SruClient,
        *,
        consortium_members: set[str] | None = None,
    ):
        self._sru = sru
        self._members = consortium_members or set()

    async def run(self, request: IllRequest) -> DiscoveryRecommendation:
        records: list[SruRecord] = []
        diagnostics: list[str] = []

        if request.item.isbn:
            records = await self._sru.search_isbn(request.item.isbn)
        elif request.item.issn:
            records = await self._sru.search_issn(request.item.issn)
        elif request.item.title:
            records = await self._sru.search_title(
                request.item.title, request.item.author
            )
        else:
            diagnostics.append("no isbn/issn/title available; cannot search")

        candidates = self._records_to_candidates(records)

        if not candidates:
            diagnostics.append("zero holders matched; saga will be Unfilled")

        rationale = self._make_rationale(request, candidates, diagnostics)
        return DiscoveryRecommendation(
            candidates=candidates,
            diagnostics=diagnostics,
            rationale=rationale,
        )

    def _records_to_candidates(self, records: list[SruRecord]) -> list[HolderCandidate]:
        seen: dict[str, HolderCandidate] = {}
        for rec in records:
            for symbol in rec.holdings:
                clean = symbol.strip()
                if not clean:
                    continue
                if clean in seen:
                    continue
                seen[clean] = HolderCandidate(
                    symbol=clean,
                    status="unknown",
                    is_consortium_member=clean in self._members,
                    preferred_score=1.0 if clean in self._members else 0.5,
                    raw={"src": "sru"},
                )
        return list(seen.values())

    @staticmethod
    def _make_rationale(
        request: IllRequest,
        candidates: list[HolderCandidate],
        diagnostics: list[str],
    ) -> str:
        if not candidates:
            return "No SRU holdings matched; recommend marking Unfilled."
        consortium = sum(1 for c in candidates if c.is_consortium_member)
        return (
            f"Found {len(candidates)} holder(s) for "
            f"{request.item.title!r}; {consortium} in-consortium."
        )
