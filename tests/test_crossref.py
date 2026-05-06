"""Unit tests for the CrossRef client.

Backed by ``httpx.MockTransport`` rather than hitting the public
endpoint. The fixture ``_works_payload()`` mirrors the real CrossRef
``works/{doi}`` envelope; verified once against
``api.crossref.org/works/10.1145/361002.361007`` before this test
landed (Bentley 1975, "Multidimensional binary search trees…", a
deliberately well-formed live record). Re-run that probe if the
parser starts disagreeing with reality.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agora.clients.crossref import (
    CrossrefRecord,
    HttpCrossrefClient,
    MockCrossrefClient,
    _extract_year,
    _first_str,
    _normalise_doi,
    _parse_message,
)
from agora.clients.errors import RemoteUnavailableError

# --- Fixtures --------------------------------------------------------------


def _works_payload() -> dict[str, Any]:
    """Realistic CrossRef envelope mirroring the live response shape."""
    return {
        "status": "ok",
        "message-type": "work",
        "message-version": "1.0.0",
        "message": {
            "DOI": "10.1145/361002.361007",
            "title": ["Multidimensional binary search trees used for associative searching"],
            "author": [
                {
                    "given": "Jon Louis",
                    "family": "Bentley",
                    "sequence": "first",
                    "affiliation": [{"name": "Stanford Univ., Stanford, CA"}],
                }
            ],
            "ISSN": ["0001-0782", "1557-7317"],
            "ISBN": None,
            "container-title": ["Communications of the ACM"],
            "published-print": {"date-parts": [[1975, 9]]},
            "type": "journal-article",
            "publisher": "Association for Computing Machinery (ACM)",
            "issue": "9",
            "volume": "18",
        },
    }


def _make_client(handler: Any, *, mailto: str | None = None) -> HttpCrossrefClient:
    """Build a client with an injected MockTransport handler."""
    return HttpCrossrefClient(
        base_url="https://api.crossref.test",
        timeout=1.0,
        mailto=mailto if mailto is not None else "",
        transport=httpx.MockTransport(handler),
    )


# --- _normalise_doi --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1145/361002.361007", "10.1145/361002.361007"),
        ("doi:10.1145/361002.361007", "10.1145/361002.361007"),
        ("doi: 10.1145/361002.361007", "10.1145/361002.361007"),
        ("DOI:10.1145/361002.361007", "10.1145/361002.361007"),
        ("https://doi.org/10.1145/361002.361007", "10.1145/361002.361007"),
        ("http://doi.org/10.1145/361002.361007", "10.1145/361002.361007"),
        ("https://dx.doi.org/10.1145/361002.361007", "10.1145/361002.361007"),
        ("  10.1145/361002.361007  ", "10.1145/361002.361007"),
    ],
)
def test_normalise_doi_strips_common_prefixes(raw: str, expected: str) -> None:
    assert _normalise_doi(raw) == expected


# --- _parse_message --------------------------------------------------------


def test_parse_message_happy_path() -> None:
    record = _parse_message(_works_payload())
    assert record is not None
    assert record.doi == "10.1145/361002.361007"
    assert record.title.startswith("Multidimensional binary search trees")
    assert record.authors == ["Jon Louis Bentley"]
    assert record.issn == "0001-0782"
    assert record.isbn is None
    assert record.container_title == "Communications of the ACM"
    assert record.year == 1975
    assert record.item_kind == "article"
    # Raw is the message dict, not the outer envelope.
    assert record.raw["DOI"] == "10.1145/361002.361007"
    assert "status" not in record.raw


def test_parse_message_missing_envelope_fields() -> None:
    # Wrong message-type → reject (defensive: list-of-works endpoint
    # returns "work-list", which we don't speak).
    assert _parse_message({"message-type": "work-list", "message": {}}) is None
    # Missing message → reject.
    assert _parse_message({"message-type": "work"}) is None
    # Non-dict input → reject.
    assert _parse_message("not a dict") is None  # type: ignore[arg-type]
    # Missing DOI inside message → reject (no stable identifier).
    assert _parse_message(
        {"message-type": "work", "message": {"title": ["x"]}}
    ) is None


def test_parse_message_handles_minimal_record() -> None:
    """Many CrossRef records lack ISSN/ISBN/container/authors. Parser
    should tolerate all-optional fields and surface defaults."""
    payload = {
        "message-type": "work",
        "message": {"DOI": "10.0000/minimal", "type": "monograph"},
    }
    record = _parse_message(payload)
    assert record is not None
    assert record.doi == "10.0000/minimal"
    assert record.title == ""
    assert record.authors == []
    assert record.issn is None
    assert record.isbn is None
    assert record.container_title is None
    assert record.year is None
    assert record.item_kind == "book"  # monograph maps to book


def test_parse_message_year_falls_back_through_date_fields() -> None:
    """``published-print`` preferred; falls back to ``published-online``,
    ``published``, then ``issued``."""

    def envelope(date_field: str, date_parts: list[list[int]]) -> dict[str, Any]:
        return {
            "message-type": "work",
            "message": {
                "DOI": "10.0/x",
                "type": "journal-article",
                date_field: {"date-parts": date_parts},
            },
        }

    rec = _parse_message(envelope("published-online", [[2020, 6, 1]]))
    assert rec is not None and rec.year == 2020

    rec = _parse_message(envelope("issued", [[2018]]))
    assert rec is not None and rec.year == 2018

    rec = _parse_message(envelope("published", [[2017, 3]]))
    assert rec is not None and rec.year == 2017


def test_parse_message_unknown_type_maps_to_other() -> None:
    payload = {
        "message-type": "work",
        "message": {"DOI": "10.0/x", "type": "dataset"},  # not in our table
    }
    record = _parse_message(payload)
    assert record is not None
    assert record.item_kind == "other"


def test_parse_message_skips_authors_with_no_name() -> None:
    payload = {
        "message-type": "work",
        "message": {
            "DOI": "10.0/x",
            "type": "journal-article",
            "author": [
                {"given": "", "family": ""},
                {"given": "Ada"},
                {"family": "Lovelace"},
                "garbage",  # not a dict — must be skipped
            ],
        },
    }
    record = _parse_message(payload)
    assert record is not None
    assert record.authors == ["Ada", "Lovelace"]


# --- HttpCrossrefClient.lookup_doi -----------------------------------------


@pytest.mark.asyncio
async def test_lookup_doi_happy_path_hits_works_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_works_payload())

    client = _make_client(handler)
    try:
        record = await client.lookup_doi("10.1145/361002.361007")
    finally:
        await client.aclose()

    assert record is not None
    assert record.doi == "10.1145/361002.361007"
    assert record.title.startswith("Multidimensional binary search trees")
    assert len(seen) == 1
    # Path is /works/{doi}; URL-bare DOI is acceptable for CrossRef.
    assert seen[0].url.path == "/works/10.1145/361002.361007"


@pytest.mark.asyncio
async def test_lookup_doi_normalises_url_form_inputs() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_works_payload())

    client = _make_client(handler)
    try:
        await client.lookup_doi("https://doi.org/10.1145/361002.361007")
    finally:
        await client.aclose()

    # The URL form was normalised before path interpolation.
    assert seen[0].url.path == "/works/10.1145/361002.361007"


@pytest.mark.asyncio
async def test_lookup_doi_returns_none_on_404() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Resource not found.")

    client = _make_client(handler)
    try:
        record = await client.lookup_doi("10.9999/does.not.exist")
    finally:
        await client.aclose()
    assert record is None


@pytest.mark.asyncio
async def test_lookup_doi_raises_on_5xx() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream temporarily unavailable")

    client = _make_client(handler)
    try:
        with pytest.raises(RemoteUnavailableError, match="503"):
            await client.lookup_doi("10.1145/361002.361007")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_lookup_doi_raises_on_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    client = _make_client(handler)
    try:
        with pytest.raises(RemoteUnavailableError, match="unreachable"):
            await client.lookup_doi("10.1145/361002.361007")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_lookup_doi_returns_none_on_malformed_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
        )

    client = _make_client(handler)
    try:
        record = await client.lookup_doi("10.1145/361002.361007")
    finally:
        await client.aclose()
    assert record is None


@pytest.mark.asyncio
async def test_lookup_doi_short_circuits_on_empty_or_invalid_doi() -> None:
    """No HTTP call when the input clearly isn't a DOI."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_works_payload())

    client = _make_client(handler)
    try:
        assert await client.lookup_doi("") is None
        assert await client.lookup_doi("   ") is None
        assert await client.lookup_doi("not-a-doi") is None  # no slash
    finally:
        await client.aclose()
    assert seen == []


# --- User-Agent / polite-pool opt-in ---------------------------------------


@pytest.mark.asyncio
async def test_user_agent_plain_when_no_mailto() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, json=_works_payload())

    client = _make_client(handler, mailto="")
    try:
        await client.lookup_doi("10.1145/361002.361007")
    finally:
        await client.aclose()
    assert seen == ["Agora/0.1"]


@pytest.mark.asyncio
async def test_user_agent_polite_when_mailto_set() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, json=_works_payload())

    client = _make_client(handler, mailto="ill-team@example.org")
    try:
        await client.lookup_doi("10.1145/361002.361007")
    finally:
        await client.aclose()
    assert seen == ["Agora/0.1 (mailto:ill-team@example.org)"]


# --- MockCrossrefClient ----------------------------------------------------


@pytest.mark.asyncio
async def test_mock_client_returns_seeded_record() -> None:
    record = CrossrefRecord(
        doi="10.0/seeded",
        title="seeded",
        authors=[],
        issn=None,
        isbn=None,
        container_title=None,
        year=None,
        item_kind="other",
        raw={},
    )
    mock = MockCrossrefClient({"10.0/seeded": record})
    assert await mock.lookup_doi("10.0/seeded") is record
    # URL form is normalised before lookup.
    assert await mock.lookup_doi("https://doi.org/10.0/seeded") is record


@pytest.mark.asyncio
async def test_mock_client_returns_none_for_unknown_doi() -> None:
    mock = MockCrossrefClient({})
    assert await mock.lookup_doi("10.0/never.seen") is None


# ---------------------------------------------------------------------------
# _first_str — line 265 (exhausted list returns None)
# ---------------------------------------------------------------------------


def test_first_str_all_non_string_items_returns_none() -> None:
    """_first_str returns None when no list item is a non-empty string (line 265)."""
    assert _first_str([None, 42, "", "  "]) is None


# ---------------------------------------------------------------------------
# _extract_year — line 274 (empty parts list)
# ---------------------------------------------------------------------------


def test_extract_year_empty_parts_list_returns_none() -> None:
    """_extract_year returns None when date-parts is an empty list (line 274)."""
    assert _extract_year({"date-parts": []}) is None


# ---------------------------------------------------------------------------
# _extract_year — line 277 (first element is empty list)
# ---------------------------------------------------------------------------


def test_extract_year_empty_first_part_returns_none() -> None:
    """_extract_year returns None when date-parts[0] is an empty list (line 277)."""
    assert _extract_year({"date-parts": [[]]}) is None


# ---------------------------------------------------------------------------
# _extract_year — lines 281-282 (digit string candidate → int)
# ---------------------------------------------------------------------------


def test_extract_year_digit_string_returns_int() -> None:
    """_extract_year converts a digit-string year to int (lines 281-282)."""
    assert _extract_year({"date-parts": [["1975"]]}) == 1975


# ---------------------------------------------------------------------------
# _extract_year — line 283 (non-digit string candidate → None)
# ---------------------------------------------------------------------------


def test_extract_year_non_digit_string_returns_none() -> None:
    """_extract_year returns None when candidate is a non-digit string (line 283)."""
    assert _extract_year({"date-parts": [["abc"]]}) is None
