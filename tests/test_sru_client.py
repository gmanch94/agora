"""Unit tests for HttpSruClient and the _parse_sru_response / _subfield helpers.

Uses ``respx`` to intercept httpx at the transport layer so no real
network calls are made.  ``asyncio_mode = "auto"`` in pyproject.toml
means async test functions are picked up without @pytest.mark.asyncio.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from agora.clients.errors import RemoteUnavailableError
from agora.clients.sru import HttpSruClient, SruRecord, _parse_sru_response

# ---------------------------------------------------------------------------
# MARCXML fixture helpers
# ---------------------------------------------------------------------------

_SRU_NS = (
    'xmlns:zs="http://www.loc.gov/zing/srw/" '
    'xmlns:marc="http://www.loc.gov/MARC21/slim"'
)

_BASE_URL = "http://sru.test"


def _wrap_marcxml(*records: str) -> str:
    """Wrap MARC record strings in an SRU SearchRetrieveResponse envelope."""
    inner = "".join(
        f"<zs:record><zs:recordData>{r}</zs:recordData></zs:record>"
        for r in records
    )
    return (
        f'<zs:searchRetrieveResponse {_SRU_NS}>'
        f"<zs:records>{inner}</zs:records>"
        f"</zs:searchRetrieveResponse>"
    )


def _marc_record(
    title: str = "Test Title",
    author: str | None = "Test Author",
    isbn: str | None = "9780000000001",
    issn: str | None = None,
    holdings: list[str] | None = None,
) -> str:
    """Build a minimal MARCXML <marc:record> string."""
    parts = ['<marc:record xmlns:marc="http://www.loc.gov/MARC21/slim">']
    parts.append(
        f'<marc:datafield tag="245" ind1=" " ind2=" ">'
        f'<marc:subfield code="a">{title}</marc:subfield>'
        f"</marc:datafield>"
    )
    if author:
        parts.append(
            f'<marc:datafield tag="100" ind1=" " ind2=" ">'
            f'<marc:subfield code="a">{author}</marc:subfield>'
            f"</marc:datafield>"
        )
    if isbn:
        parts.append(
            f'<marc:datafield tag="020" ind1=" " ind2=" ">'
            f'<marc:subfield code="a">{isbn}</marc:subfield>'
            f"</marc:datafield>"
        )
    if issn:
        parts.append(
            f'<marc:datafield tag="022" ind1=" " ind2=" ">'
            f'<marc:subfield code="a">{issn}</marc:subfield>'
            f"</marc:datafield>"
        )
    for loc in holdings or []:
        parts.append(
            f'<marc:datafield tag="852" ind1=" " ind2=" ">'
            f'<marc:subfield code="a">{loc}</marc:subfield>'
            f"</marc:datafield>"
        )
    parts.append("</marc:record>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# _parse_sru_response — pure-Python unit tests (no HTTP)
# ---------------------------------------------------------------------------


def test_parse_sru_response_full_record() -> None:
    xml = _wrap_marcxml(_marc_record(holdings=["LIB-A", "LIB-B"]))
    records = _parse_sru_response(xml)
    assert len(records) == 1
    rec = records[0]
    assert rec.title == "Test Title"
    assert rec.authors == ["Test Author"]
    assert rec.isbn == "9780000000001"
    assert rec.issn is None
    assert rec.holdings == ["LIB-A", "LIB-B"]
    assert rec.raw_marcxml  # serialised by lxml — must be non-empty


def test_parse_sru_response_no_author_or_isbn() -> None:
    xml = _wrap_marcxml(_marc_record(author=None, isbn=None))
    records = _parse_sru_response(xml)
    assert len(records) == 1
    assert records[0].authors == []
    assert records[0].isbn is None


def test_parse_sru_response_issn_field() -> None:
    xml = _wrap_marcxml(_marc_record(isbn=None, issn="1234-5678"))
    records = _parse_sru_response(xml)
    assert records[0].issn == "1234-5678"


def test_parse_sru_response_multiple_records() -> None:
    xml = _wrap_marcxml(
        _marc_record(title="Alpha"),
        _marc_record(title="Beta"),
    )
    records = _parse_sru_response(xml)
    assert len(records) == 2
    assert {r.title for r in records} == {"Alpha", "Beta"}


def test_parse_sru_response_empty_records_element() -> None:
    xml = f'<zs:searchRetrieveResponse {_SRU_NS}><zs:records/></zs:searchRetrieveResponse>'
    assert _parse_sru_response(xml) == []


def test_parse_sru_response_invalid_xml_returns_empty() -> None:
    assert _parse_sru_response("not xml at all <<>>") == []


# ---------------------------------------------------------------------------
# HttpSruClient — network tests via respx
# ---------------------------------------------------------------------------


@respx.mock
async def test_search_isbn_sends_correct_cql() -> None:
    route = respx.get(_BASE_URL).mock(
        return_value=Response(200, text=_wrap_marcxml(_marc_record()))
    )
    client = HttpSruClient(base_url=_BASE_URL, timeout=5.0)
    try:
        results = await client.search_isbn("9780000000001")
    finally:
        await client.aclose()

    assert route.called
    params = dict(route.calls.last.request.url.params)
    assert params["query"] == "bath.isbn=9780000000001"
    assert params["operation"] == "searchRetrieve"
    assert len(results) == 1
    assert isinstance(results[0], SruRecord)


@respx.mock
async def test_search_issn_sends_correct_cql() -> None:
    route = respx.get(_BASE_URL).mock(
        return_value=Response(200, text=_wrap_marcxml(_marc_record(issn="1234-5678")))
    )
    client = HttpSruClient(base_url=_BASE_URL, timeout=5.0)
    try:
        results = await client.search_issn("1234-5678")
    finally:
        await client.aclose()

    params = dict(route.calls.last.request.url.params)
    assert params["query"] == "bath.issn=1234-5678"
    assert len(results) == 1


@respx.mock
async def test_search_title_without_author() -> None:
    route = respx.get(_BASE_URL).mock(
        return_value=Response(200, text=_wrap_marcxml(_marc_record()))
    )
    client = HttpSruClient(base_url=_BASE_URL, timeout=5.0)
    try:
        await client.search_title("Brave New World")
    finally:
        await client.aclose()

    params = dict(route.calls.last.request.url.params)
    assert params["query"] == 'dc.title="Brave New World"'


@respx.mock
async def test_search_title_with_author_appends_creator() -> None:
    route = respx.get(_BASE_URL).mock(
        return_value=Response(200, text=_wrap_marcxml(_marc_record()))
    )
    client = HttpSruClient(base_url=_BASE_URL, timeout=5.0)
    try:
        await client.search_title("Brave New World", author="Huxley")
    finally:
        await client.aclose()

    params = dict(route.calls.last.request.url.params)
    assert params["query"] == 'dc.title="Brave New World" and dc.creator="Huxley"'


@respx.mock
async def test_server_error_raises_remote_unavailable() -> None:
    respx.get(_BASE_URL).mock(return_value=Response(503))
    client = HttpSruClient(base_url=_BASE_URL, timeout=5.0)
    try:
        with pytest.raises(RemoteUnavailableError, match="SRU 503"):
            await client.search_isbn("9780000000001")
    finally:
        await client.aclose()


@respx.mock
async def test_network_error_raises_remote_unavailable() -> None:
    respx.get(_BASE_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    client = HttpSruClient(base_url=_BASE_URL, timeout=5.0)
    try:
        with pytest.raises(RemoteUnavailableError):
            await client.search_isbn("9780000000001")
    finally:
        await client.aclose()
