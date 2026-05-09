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


# ---------------------------------------------------------------------------
# Audit 2026-05-09 #6 / #18: XXE / billion-laughs hardening
# ---------------------------------------------------------------------------


def test_parse_sru_response_blocks_external_entity_expansion() -> None:
    """A MARCXML doc declaring a file:// external entity must NOT resolve it.

    Pre-fix the SRU parser used the bare default lxml parser, which
    resolves entities by default. A malicious or compromised SRU peer
    could deliver a payload like::

        <!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        ... &xxe; ...

    and exfiltrate local files via the parsed-record return path.
    Post-fix (``SAFE_XML_PARSER`` from ``agora.clients._xml``) the
    parser refuses entity resolution; either the parse fails or the
    entity expands to the empty string. Either way no file content
    leaks into ``raw_marcxml`` or any other returned field.
    """
    xxe_payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE foo ['
        '<!ENTITY xxe SYSTEM "file:///etc/passwd">'
        ']>'
        f'<zs:searchRetrieveResponse {_SRU_NS}>'
        "<zs:records><zs:record><zs:recordData>"
        '<marc:record xmlns:marc="http://www.loc.gov/MARC21/slim">'
        '<marc:datafield tag="245"><marc:subfield code="a">&xxe;</marc:subfield></marc:datafield>'
        "</marc:record>"
        "</zs:recordData></zs:record></zs:records>"
        "</zs:searchRetrieveResponse>"
    )

    records = _parse_sru_response(xxe_payload)

    # Either the doc is rejected as a syntax error (lxml refuses the
    # DTD with no_network=True + resolve_entities=False) and we get an
    # empty list, OR it parses but the entity expands to nothing — in
    # which case the title field must NOT contain anything that looks
    # like an /etc/passwd line.
    for r in records:
        assert "root:" not in r.title
        assert "/bin/" not in r.title
        assert "root:" not in r.raw_marcxml
        assert "/bin/" not in r.raw_marcxml


def test_safe_xml_parser_blocks_billion_laughs_amplification() -> None:
    """The shared parser refuses pathologically-amplified entity expansion.

    A mild "billion laughs"-style payload (entity-of-entities, fewer
    levels than a true attack so the test runs fast) must NOT expand
    when parsed with ``SAFE_XML_PARSER``. lxml raises XMLSyntaxError
    on entity references when ``resolve_entities=False``, which the
    SRU client's caller catches as "no records."
    """
    from lxml import etree

    from agora.clients._xml import SAFE_XML_PARSER

    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY a "AAA">'
        '<!ENTITY b "&a;&a;&a;">]>'
        "<doc>&b;</doc>"
    )
    # Either the parser raises, or it parses and refuses to expand the
    # entity (returning the literal entity text or empty). Both are
    # acceptable; the unsafe behaviour would expand "&b;" → "AAAAAAAAA".
    try:
        root = etree.fromstring(payload.encode(), SAFE_XML_PARSER)
    except etree.XMLSyntaxError:
        return
    assert "AAA" not in (root.text or "")
