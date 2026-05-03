"""DiscoveryAgent integration tests — CrossRef + SRU pipeline.

Distinct from ``tests/test_agents.py::test_discovery_returns_consortium_first``
(which validates SRU-only behavior). These tests pin the CrossRef
integration semantics introduced in PR-B:

- CrossRef-confirmed identifiers take precedence over the request's
  own (the patron may have pasted a DOI without a matching ISSN).
- CrossRef hiccups (404 / 5xx / network) MUST NOT prevent SRU
  fallback from running — discovery is best-effort identity, hard
  requirement on holders.
- ``request.item`` is never mutated — saga input is durable.

Tests use ``MockCrossrefClient`` (in-memory map) and ``MockSruClient``
(deterministic fixture). For the unavailable-error path we hand-roll
a tiny throwing client because the mock is success-or-empty by design.
"""

from __future__ import annotations

import pytest

from agora.agents.discovery import DiscoveryAgent
from agora.clients.crossref import (
    CrossrefClient,
    CrossrefRecord,
    MockCrossrefClient,
)
from agora.clients.errors import RemoteUnavailableError
from agora.clients.sru import MockSruClient, SruRecord
from agora.models.request import (
    IllRequest,
    ItemMetadata,
    LibraryRef,
    PatronRef,
    RequestType,
)

# --- Fixtures --------------------------------------------------------------


def _request(
    *,
    title: str = "Test",
    doi: str | None = None,
    isbn: str | None = None,
    issn: str | None = None,
) -> IllRequest:
    """Build an IllRequest with explicit item-field overrides.

    Typed kwargs (rather than ``**Any``) keep mypy --strict happy and
    surface typos at the call site.
    """
    return IllRequest(
        request_type=RequestType.LOAN,
        patron=PatronRef(library_symbol="A", patron_id="p1"),
        requesting_library=LibraryRef(symbol="A"),
        item=ItemMetadata(title=title, doi=doi, isbn=isbn, issn=issn),
    )


def _cr_record(
    doi: str = "10.1145/361002.361007",
    *,
    title: str = "Multidimensional binary search trees",
    issn: str | None = "0001-0782",
    isbn: str | None = None,
    container: str | None = "Communications of the ACM",
    year: int | None = 1975,
) -> CrossrefRecord:
    return CrossrefRecord(
        doi=doi,
        title=title,
        authors=["Jon Louis Bentley"],
        issn=issn,
        isbn=isbn,
        container_title=container,
        year=year,
        item_kind="article",
        raw={},
    )


class _SpySruClient:
    """SRU client that records which search method was called.

    Wraps a MockSruClient so we can assert on (method, arg) without
    losing the deterministic record-return behavior. The advisor's
    test #6 ("DOI absent → CrossRef never invoked") uses the
    CrossRef-side spy below; this is the SRU-side mirror for tests
    that need to assert on which identifier seeded the search.
    """

    def __init__(self, records: list[SruRecord] | None = None):
        self._inner = MockSruClient(records=records)
        self.calls: list[tuple[str, str | None]] = []

    async def search_isbn(self, isbn: str) -> list[SruRecord]:
        self.calls.append(("isbn", isbn))
        return await self._inner.search_isbn(isbn)

    async def search_issn(self, issn: str) -> list[SruRecord]:
        self.calls.append(("issn", issn))
        return await self._inner.search_issn(issn)

    async def search_title(
        self, title: str, author: str | None = None
    ) -> list[SruRecord]:
        self.calls.append(("title", title))
        return await self._inner.search_title(title, author)


class _SpyCrossrefClient:
    """CrossRef client recording call count, returning a fixed record."""

    def __init__(self, record: CrossrefRecord | None) -> None:
        self._record = record
        self.calls: list[str] = []

    async def lookup_doi(self, doi: str) -> CrossrefRecord | None:
        self.calls.append(doi)
        return self._record


class _UnavailableCrossrefClient:
    """CrossRef client that always raises. Models 5xx / network failure."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def lookup_doi(self, doi: str) -> CrossrefRecord | None:
        self.calls.append(doi)
        raise RemoteUnavailableError("crossref 503")


# --- 1. DOI + CrossRef hit with ISSN → SRU.search_issn keyed off CrossRef ---


@pytest.mark.asyncio
async def test_crossref_issn_overrides_request_issn() -> None:
    """The patron's wrong ISSN must not poison the SRU search when
    CrossRef returned an authoritative one."""
    cr_record = _cr_record(issn="0001-0782")  # CrossRef truth
    crossref: CrossrefClient = MockCrossrefClient(
        {"10.1145/361002.361007": cr_record}
    )
    sru = _SpySruClient(
        records=[
            SruRecord(
                title="CACM article",
                authors=["Bentley"],
                isbn=None,
                issn="0001-0782",
                holdings=["MEMBER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru, crossref=crossref, consortium_members={"MEMBER1"})

    rec = await agent.run(
        _request(
            title="some article",
            doi="10.1145/361002.361007",
            issn="9999-9999",  # patron typo
        )
    )

    assert sru.calls == [("issn", "0001-0782")]  # CrossRef won
    assert rec.has_candidates
    assert "CrossRef confirmed" in rec.rationale


# --- 2. DOI + CrossRef hit with ISBN (no ISSN) → search_isbn ---------------


@pytest.mark.asyncio
async def test_crossref_isbn_drives_sru_when_issn_absent() -> None:
    """ISBN beats ISSN in the search-method preference order."""
    cr_record = _cr_record(issn=None, isbn="9780000000001")
    crossref: CrossrefClient = MockCrossrefClient({"10.0/x": cr_record})
    sru = _SpySruClient(
        records=[
            SruRecord(
                title="Some Book",
                authors=[],
                isbn="9780000000001",
                issn=None,
                holdings=["MEMBER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru, crossref=crossref)

    await agent.run(_request(title="x", doi="10.0/x"))

    assert sru.calls == [("isbn", "9780000000001")]


# --- 3. DOI + CrossRef returns None → fall back to request identifiers ----


@pytest.mark.asyncio
async def test_crossref_miss_falls_back_to_request_identifiers() -> None:
    """A DOI not registered with CrossRef must not block the SRU
    search keyed off whatever identifier the patron typed."""
    crossref: CrossrefClient = MockCrossrefClient({})  # nothing seeded
    sru = _SpySruClient(
        records=[
            SruRecord(
                title="Test",
                authors=[],
                isbn="9780060850524",
                issn=None,
                holdings=["MEMBER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru, crossref=crossref)

    rec = await agent.run(
        _request(title="Test", doi="10.0/never.seen", isbn="9780060850524")
    )

    assert sru.calls == [("isbn", "9780060850524")]
    assert any("crossref returned no record" in d for d in rec.diagnostics)
    assert rec.has_candidates


# --- 4. DOI + CrossRef raises → fall back, exception NOT propagated --------


@pytest.mark.asyncio
async def test_crossref_unavailable_does_not_propagate() -> None:
    """A CrossRef 5xx / network failure must NOT bubble up — the saga
    still wants holders even without confirmed identity."""
    crossref = _UnavailableCrossrefClient()
    sru = _SpySruClient(
        records=[
            SruRecord(
                title="Test",
                authors=[],
                isbn="9780000000001",
                issn=None,
                holdings=["MEMBER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru, crossref=crossref)

    rec = await agent.run(
        _request(title="Test", doi="10.0/x", isbn="9780000000001")
    )

    assert crossref.calls == ["10.0/x"]  # we did try
    assert sru.calls == [("isbn", "9780000000001")]
    assert any("crossref unavailable" in d for d in rec.diagnostics)
    assert rec.has_candidates


# --- 5. DOI present but agent built without CrossRef → backward-compat ----


@pytest.mark.asyncio
async def test_no_crossref_client_means_no_lookup_even_with_doi() -> None:
    """Pre-PR-B callers (DiscoveryAgent(sru, consortium_members={...}))
    must keep working unchanged. No CrossRef client → no lookup, no
    new diagnostic, no rationale provenance line."""
    sru = _SpySruClient(
        records=[
            SruRecord(
                title="Test",
                authors=[],
                isbn="9780000000001",
                issn=None,
                holdings=["MEMBER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru)  # no crossref kwarg — pre-PR-B shape

    rec = await agent.run(
        _request(title="Test", doi="10.0/x", isbn="9780000000001")
    )

    assert sru.calls == [("isbn", "9780000000001")]
    assert "CrossRef" not in rec.rationale
    # No CrossRef diagnostics either.
    assert not any("crossref" in d.lower() for d in rec.diagnostics)


# --- 6. No DOI → CrossRef.lookup_doi never invoked -------------------------


@pytest.mark.asyncio
async def test_crossref_not_called_when_no_doi() -> None:
    """Spy assertion: ``lookup_doi`` MUST NOT be invoked when the
    patron didn't supply a DOI. Saves API budget and avoids polluting
    the polite-pool counters."""
    crossref = _SpyCrossrefClient(record=None)
    sru = _SpySruClient(
        records=[
            SruRecord(
                title="Test",
                authors=[],
                isbn="9780000000001",
                issn=None,
                holdings=["MEMBER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru, crossref=crossref)

    await agent.run(_request(title="Test", isbn="9780000000001"))  # no doi

    assert crossref.calls == []  # zero calls
    assert sru.calls == [("isbn", "9780000000001")]


# --- 7. DOI-only, CrossRef miss, no other identifier → clean diagnostic ----


@pytest.mark.asyncio
async def test_doi_only_with_crossref_miss_yields_clean_diagnostic() -> None:
    """Edge case: patron pasted a DOI we don't recognise and supplied
    nothing else. Must NOT crash; must produce a candidate-empty
    recommendation with explanatory diagnostics."""
    crossref: CrossrefClient = MockCrossrefClient({})
    sru = _SpySruClient(records=[])
    agent = DiscoveryAgent(sru, crossref=crossref)

    rec = await agent.run(
        # title="" + no isbn/issn — the only signal is the DOI
        _request(title="", doi="10.0/never.seen")
    )

    assert sru.calls == []  # nothing to search by
    assert not rec.has_candidates
    diag_text = " | ".join(rec.diagnostics)
    assert "crossref returned no record" in diag_text
    assert "no isbn/issn/title" in diag_text
    assert "Unfilled" in rec.rationale


# --- 8. Rationale provenance when CrossRef confirmed -----------------------


@pytest.mark.asyncio
async def test_rationale_mentions_crossref_when_used() -> None:
    """Staff console reads ``rationale`` to understand the
    recommendation. CrossRef provenance must be visible there, not
    just in diagnostics."""
    cr_record = _cr_record()
    crossref: CrossrefClient = MockCrossrefClient(
        {"10.1145/361002.361007": cr_record}
    )
    sru = _SpySruClient(
        records=[
            SruRecord(
                title="x",
                authors=[],
                isbn=None,
                issn="0001-0782",
                holdings=["MEMBER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru, crossref=crossref, consortium_members={"MEMBER1"})

    rec = await agent.run(_request(title="x", doi="10.1145/361002.361007"))

    assert "CrossRef confirmed" in rec.rationale
    assert "10.1145/361002.361007" in rec.rationale
    assert "Communications of the ACM" in rec.rationale  # container shown
    assert "1975" in rec.rationale
    assert "issn=0001-0782" in rec.rationale  # search seed surfaced


# --- 9. Request item is NOT mutated by enrichment --------------------------


@pytest.mark.asyncio
async def test_request_item_is_not_mutated() -> None:
    """Saga durability invariant: the IllRequest going into the saga
    must equal the IllRequest going out. CrossRef enrichment is
    runtime-only; it never rewrites the patron's submitted metadata."""
    cr_record = _cr_record(issn="0001-0782")
    crossref: CrossrefClient = MockCrossrefClient({"10.0/x": cr_record})
    sru = _SpySruClient(
        records=[
            SruRecord(
                title="x",
                authors=[],
                isbn=None,
                issn="0001-0782",
                holdings=["MEMBER1"],
                raw_marcxml="",
            )
        ]
    )
    agent = DiscoveryAgent(sru, crossref=crossref)

    req = _request(title="x", doi="10.0/x", issn="9999-9999")
    issn_before = req.item.issn
    title_before = req.item.title

    await agent.run(req)

    assert req.item.issn == issn_before == "9999-9999"  # unchanged
    assert req.item.title == title_before == "x"
