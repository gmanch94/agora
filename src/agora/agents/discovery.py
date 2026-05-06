"""DiscoveryAgent — resolve a citation to candidate holders.

This is a deterministic rules-driven agent in the prototype. Two
clients, two roles:

- ``CrossrefClient`` confirms *bibliographic identity* when the patron
  supplied a DOI: it returns the canonical title / ISSN / ISBN /
  container / year for the work. CrossRef knows nothing about
  holdings, so it cannot rank suppliers; what it does is sharpen the
  identifier we hand to SRU. A patron who pasted a DOI may have
  omitted (or guessed) the ISSN — CrossRef-confirmed ISSN is
  authoritative.
- ``SruClient`` finds *who holds* the item via MARC 852 subfields.
  This is the source of the candidate list.

Therefore this agent is **not** a "merge two ranked lists" job; it is
a sequential pipeline (CrossRef → identifier choice → SRU). The
candidate list is always SRU-derived; CrossRef enrichment only
changes which identifier the SRU search keys off.

CrossRef is best-effort. A 404 (DOI unknown to CrossRef), a 5xx, or a
network error must NOT prevent the SRU fallback from running — we
still want to find the book, even if we can't confirm its identity.
``RemoteUnavailableError`` is caught here and surfaced as a
diagnostic, not a failure.

No LLM call here — discovery is a search-and-merge problem, not a
judgement problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agora.clients.crossref import CrossrefClient, CrossrefRecord
from agora.clients.errors import RemoteUnavailableError
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
    """Search SRU (and optionally CrossRef) for holders of the requested item.

    ``crossref`` is optional. Existing callers that constructed the
    agent with only the SRU client keep working unchanged — without
    a CrossRef client, DOI inputs are passed through as today (no
    identity confirmation, just whatever ISBN/ISSN/title the patron
    typed).

    **Holdings source.** SRU is searched for MARC 852 subfield-a
    symbols. In practice, publicly accessible union catalogs that carry
    852 holdings do not exist: national-library SRU targets (DNB,
    SUDOC, LoC) are bibliographic-only; WorldCat requires a paid OCLC
    subscription. When SRU returns no 852 holdings the agent falls back
    to synthesising candidates from ``consortium_members`` with
    ``status='unverified_holdings'``, allowing the POC to route requests
    within the configured roster even without live holdings data.
    """

    def __init__(
        self,
        sru: SruClient,
        *,
        crossref: CrossrefClient | None = None,
        consortium_members: set[str] | None = None,
    ):
        self._sru = sru
        self._crossref = crossref
        self._members = consortium_members or set()

    async def run(self, request: IllRequest) -> DiscoveryRecommendation:
        diagnostics: list[str] = []

        # --- Step 1: optional CrossRef identity confirmation. ----------
        # CrossRef is consulted iff the patron supplied a DOI AND the
        # agent was built with a CrossRef client. Errors (5xx,
        # network) are downgraded to diagnostics so SRU still runs.
        cr_record = await self._maybe_lookup_crossref(request, diagnostics)

        # --- Step 2: choose effective identifiers for SRU search. -----
        # CrossRef-confirmed identifiers take precedence (the patron's
        # input may have been wrong or incomplete for DOI-only
        # submissions). The request itself is NOT mutated — saga
        # input is durable.
        eff_isbn, eff_issn, eff_title, eff_author = self._effective_identifiers(
            request, cr_record
        )

        # --- Step 3: SRU search via the most-specific identifier. -----
        records: list[SruRecord] = []
        if eff_isbn:
            records = await self._sru.search_isbn(eff_isbn)
        elif eff_issn:
            records = await self._sru.search_issn(eff_issn)
        elif eff_title:
            records = await self._sru.search_title(eff_title, eff_author)
        else:
            # No usable identifier survived CrossRef + the request.
            # Most likely path: DOI-only request whose CrossRef
            # lookup returned None and produced no fallback ISSN/ISBN.
            diagnostics.append(
                "no isbn/issn/title available for SRU; cannot search"
            )

        # --- Step 4: dedupe holders into candidates. ------------------
        candidates = self._records_to_candidates(records)

        if not candidates:
            diagnostics.append("zero holders matched; saga will be Unfilled")
        elif any(c.status == "unverified_holdings" for c in candidates):
            diagnostics.append(
                f"SRU returned no 852 holdings; falling back to "
                f"{len(candidates)} consortium member(s) as unverified candidates"
            )

        rationale = self._make_rationale(
            request, candidates, cr_record, eff_isbn, eff_issn, eff_title
        )
        return DiscoveryRecommendation(
            candidates=candidates,
            diagnostics=diagnostics,
            rationale=rationale,
        )

    async def _maybe_lookup_crossref(
        self, request: IllRequest, diagnostics: list[str]
    ) -> CrossrefRecord | None:
        """Best-effort CrossRef DOI → identity lookup.

        Returns ``None`` when: no DOI on the request, no CrossRef
        client configured, the DOI was not registered with CrossRef,
        or CrossRef was unreachable. The last two paths emit a
        diagnostic so staff can see why identity wasn't confirmed.
        Never raises — a CrossRef hiccup must not break discovery.
        """
        if self._crossref is None:
            return None
        doi = request.item.doi
        if not doi:
            return None
        try:
            record = await self._crossref.lookup_doi(doi)
        except RemoteUnavailableError as exc:
            # Hard upstream failure. Log via diagnostic and let SRU
            # carry on with the patron's original identifiers.
            diagnostics.append(
                f"crossref unavailable; falling back to SRU ({exc})"
            )
            return None
        if record is None:
            diagnostics.append(
                f"crossref returned no record for doi={doi}"
            )
            return None
        return record

    @staticmethod
    def _effective_identifiers(
        request: IllRequest, cr: CrossrefRecord | None
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Compute (isbn, issn, title, author) used to search SRU.

        Preference order: CrossRef-confirmed value, then the request's
        own value. CrossRef is authoritative for identity when it
        answered — the patron's typed ISSN may have been wrong/missing
        for a DOI-paste flow. ``request.item`` is intentionally not
        mutated.
        """
        if cr is None:
            return (
                request.item.isbn,
                request.item.issn,
                request.item.title or None,
                request.item.author,
            )
        return (
            cr.isbn or request.item.isbn,
            cr.issn or request.item.issn,
            cr.title or request.item.title or None,
            request.item.author,  # CrossRef has authors but we don't
                                   # currently use them for SRU CQL —
                                   # let the patron's hint stand.
        )

    def _records_to_candidates(self, records: list[SruRecord]) -> list[HolderCandidate]:
        """Deduplicate MARC 852 holders into ``HolderCandidate`` objects.

        When SRU records carry no 852 holdings (bibliographic-only catalogs)
        AND the agent was built with a non-empty ``consortium_members`` set,
        every member is synthesised as a candidate with
        ``status='unverified_holdings'``.  This lets the POC route within the
        configured consortium even without live holdings data — WorldCat or a
        freely accessible union catalog would supply verified coverage in
        production.
        """
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

        if seen:
            return list(seen.values())

        # No 852 holdings found via SRU.  Fall back to the configured
        # consortium roster so the POC can still route without a paid
        # union-catalog subscription.
        if self._members:
            return [
                HolderCandidate(
                    symbol=m,
                    status="unverified_holdings",
                    is_consortium_member=True,
                    preferred_score=0.5,
                    raw={"src": "consortium_fallback"},
                )
                for m in sorted(self._members)
            ]

        return []

    @staticmethod
    def _make_rationale(
        request: IllRequest,
        candidates: list[HolderCandidate],
        cr: CrossrefRecord | None,
        eff_isbn: str | None,
        eff_issn: str | None,
        eff_title: str | None,
    ) -> str:
        # Provenance prefix: tell staff if CrossRef confirmed identity
        # and what identifier seeded the SRU search. Helps reviewers
        # spot when the patron's metadata was wrong.
        parts: list[str] = []
        if cr is not None:
            container = f" ({cr.container_title})" if cr.container_title else ""
            year = f" {cr.year}" if cr.year else ""
            parts.append(
                f"CrossRef confirmed doi={cr.doi} → {cr.title!r}{container}{year}."
            )
        seed = (
            f"isbn={eff_isbn}" if eff_isbn
            else f"issn={eff_issn}" if eff_issn
            else f"title={eff_title!r}" if eff_title
            else "no identifier"
        )
        if not candidates:
            parts.append(
                f"No SRU holdings matched (seed: {seed}); recommend marking Unfilled."
            )
            return " ".join(parts)
        # Consortium fallback path: SRU returned no 852 holdings so the
        # roster was used directly.  Make the caveat explicit in rationale.
        if all(c.status == "unverified_holdings" for c in candidates):
            n = len(candidates)
            parts.append(
                f"SRU search (seed: {seed}) returned no 852 holdings — "
                f"no freely accessible union catalog available. "
                f"Synthesised {n} consortium member(s) as unverified candidates; "
                f"holdings not confirmed."
            )
            return " ".join(parts)
        # Normal SRU path: at least one 852 holder was found.
        consortium = sum(1 for c in candidates if c.is_consortium_member)
        title_for_msg = (cr.title if cr else None) or request.item.title
        parts.append(
            f"Found {len(candidates)} holder(s) for {title_for_msg!r} via SRU "
            f"(seed: {seed}); {consortium} in-consortium."
        )
        return " ".join(parts)
