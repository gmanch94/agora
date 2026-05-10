"""Unit tests for HttpNcipClient — NCIP 2.0 XML client (source-review verified).

Verifies that:
- check_out POSTs a well-formed CheckOutItem NCIPMessage to /ncip
- check_in  POSTs a well-formed CheckInItem  NCIPMessage to /ncip
- XML constants (_NS, _VERSION_ATTR) match the verified values
- Required headers (X-Okapi-Tenant, Content-Type, Idempotency-Key) are sent
- HTTP 5xx responses raise NcipError (infrastructure failure)
- HTTP 200 + Problem element raises NcipError (application failure)
- health() GETs /admin/health and returns True on 200

Uses ``respx`` to intercept httpx at the transport layer. XML assertions
parse the sent body with lxml (safe parser) to avoid ordering sensitivity.
"""

from __future__ import annotations

import respx
from httpx import Response
from lxml import etree

from agora.clients._xml import SAFE_XML_PARSER as _XML_PARSER
from agora.clients.ncip import (
    _NS,
    _VERSION_ATTR,
    HttpNcipClient,
    NcipError,
    NcipResult,
)
from agora.config import Settings

_BASE = "http://mod-ncip.test"
_TENANT = "diku"
_AGENCY = "TEST-LIB"
_ITEM_ID = "barcode-0042"
_PATRON_ID = "patron-8377630"
_IDEM_KEY = "ncip-test-001"

_SETTINGS = Settings(
    NCIP_BASE_URL=_BASE,
    NCIP_AGENCY_ID=_AGENCY,
    RESHARE_BASE_URL="",
    RESHARE_TENANT=_TENANT,
    RESHARE_USER="",
    RESHARE_PASSWORD="",
    OKAPI_URL="",
)

# Minimal success response (HTTP 200, no Problem element).
_SUCCESS_CHECKOUT = (
    f'<?xml version="1.0" encoding="UTF-8"?>'
    f'<NCIPMessage version="{_VERSION_ATTR}" xmlns="{_NS}">'
    f"<CheckOutItemResponse/>"
    f"</NCIPMessage>"
).encode()

_SUCCESS_CHECKIN = (
    f'<?xml version="1.0" encoding="UTF-8"?>'
    f'<NCIPMessage version="{_VERSION_ATTR}" xmlns="{_NS}">'
    f"<CheckInItemResponse/>"
    f"</NCIPMessage>"
).encode()

# Application-level failure (HTTP 200 + Problem inside response).
_APP_PROBLEM = (
    f'<?xml version="1.0" encoding="UTF-8"?>'
    f'<NCIPMessage version="{_VERSION_ATTR}" xmlns="{_NS}">'
    f"<CheckOutItemResponse>"
    f"<Problem>"
    f"<ProblemValue>ITEM_NOT_CHECKED_OUT</ProblemValue>"
    f"</Problem>"
    f"</CheckOutItemResponse>"
    f"</NCIPMessage>"
).encode()

# Infrastructure failure (HTTP 500, raw Problem, no NCIP namespace).
_INFRA_PROBLEM = b"<Problem><message>problem processing NCIP request</message></Problem>"


def _parse_sent_xml(body: bytes) -> etree._Element:
    """Parse XML sent in a respx call using the safe parser."""
    return etree.fromstring(body, _XML_PARSER)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_ncip_namespace_constant() -> None:
    """_NS matches the NISO 2008 NCIP namespace used in all wire samples."""
    assert _NS == "http://www.niso.org/2008/ncip"


def test_ncip_version_attr_constant() -> None:
    """_VERSION_ATTR matches the NCIP 2.0 schema URI used in all wire samples."""
    assert _VERSION_ATTR == "http://www.niso.org/schemas/ncip/v2_0/ncip_v2_0.xsd"


# ---------------------------------------------------------------------------
# check_out
# ---------------------------------------------------------------------------


@respx.mock
async def test_checkout_posts_to_ncip_endpoint() -> None:
    """check_out POSTs to /ncip."""
    route = respx.post(f"{_BASE}/ncip").mock(
        return_value=Response(200, content=_SUCCESS_CHECKOUT)
    )

    client = HttpNcipClient(_SETTINGS)
    try:
        await client.check_out(
            idempotency_key=_IDEM_KEY, item_id=_ITEM_ID, patron_id=_PATRON_ID
        )
    finally:
        await client.aclose()

    assert route.called
    assert route.call_count == 1


@respx.mock
async def test_checkout_sends_checkout_item_element() -> None:
    """check_out XML body contains a CheckOutItem in the NCIP namespace."""
    route = respx.post(f"{_BASE}/ncip").mock(
        return_value=Response(200, content=_SUCCESS_CHECKOUT)
    )

    client = HttpNcipClient(_SETTINGS)
    try:
        await client.check_out(
            idempotency_key=_IDEM_KEY, item_id=_ITEM_ID, patron_id=_PATRON_ID
        )
    finally:
        await client.aclose()

    body = _parse_sent_xml(route.calls[0].request.content)
    # Root must be NCIPMessage with version attr
    assert body.tag == f"{{{_NS}}}NCIPMessage"
    assert body.get("version") == _VERSION_ATTR
    # Must have a CheckOutItem child
    checkout = body.find(f"{{{_NS}}}CheckOutItem")
    assert checkout is not None, "CheckOutItem element missing"


@respx.mock
async def test_checkout_xml_contains_patron_and_item_ids() -> None:
    """CheckOutItem XML embeds patron and item barcodes in the correct elements."""
    route = respx.post(f"{_BASE}/ncip").mock(
        return_value=Response(200, content=_SUCCESS_CHECKOUT)
    )

    client = HttpNcipClient(_SETTINGS)
    try:
        await client.check_out(
            idempotency_key=_IDEM_KEY, item_id=_ITEM_ID, patron_id=_PATRON_ID
        )
    finally:
        await client.aclose()

    body = _parse_sent_xml(route.calls[0].request.content)
    patron_val = body.findtext(
        f".//{{{_NS}}}UserId/{{{_NS}}}UserIdentifierValue"
    )
    item_val = body.findtext(
        f".//{{{_NS}}}ItemId/{{{_NS}}}ItemIdentifierValue"
    )
    assert patron_val == _PATRON_ID, f"UserIdentifierValue mismatch: {patron_val!r}"
    assert item_val == _ITEM_ID, f"ItemIdentifierValue mismatch: {item_val!r}"


@respx.mock
async def test_checkout_xml_has_initiation_header_with_agency() -> None:
    """CheckOutItem includes InitiationHeader with FromAgencyId / ToAgencyId."""
    route = respx.post(f"{_BASE}/ncip").mock(
        return_value=Response(200, content=_SUCCESS_CHECKOUT)
    )

    client = HttpNcipClient(_SETTINGS)
    try:
        await client.check_out(
            idempotency_key=_IDEM_KEY, item_id=_ITEM_ID, patron_id=_PATRON_ID
        )
    finally:
        await client.aclose()

    body = _parse_sent_xml(route.calls[0].request.content)
    checkout = body.find(f"{{{_NS}}}CheckOutItem")
    assert checkout is not None
    from_agency = checkout.findtext(
        f"{{{_NS}}}InitiationHeader/{{{_NS}}}FromAgencyId/{{{_NS}}}AgencyId"
    )
    to_agency = checkout.findtext(
        f"{{{_NS}}}InitiationHeader/{{{_NS}}}ToAgencyId/{{{_NS}}}AgencyId"
    )
    assert from_agency == _AGENCY
    assert to_agency == _AGENCY


@respx.mock
async def test_checkout_sends_required_headers() -> None:
    """X-Okapi-Tenant, Content-Type, and Idempotency-Key headers are sent."""
    respx.post(f"{_BASE}/ncip").mock(return_value=Response(200, content=_SUCCESS_CHECKOUT))

    client = HttpNcipClient(_SETTINGS)
    try:
        await client.check_out(
            idempotency_key=_IDEM_KEY, item_id=_ITEM_ID, patron_id=_PATRON_ID
        )
    finally:
        await client.aclose()

    req = respx.calls[0].request
    assert req.headers["X-Okapi-Tenant"] == _TENANT
    assert req.headers["Content-Type"] == "application/xml"
    assert req.headers["Idempotency-Key"] == _IDEM_KEY


@respx.mock
async def test_checkout_returns_ncip_result() -> None:
    """check_out returns NcipResult with state='checked_out' and correct ids."""
    respx.post(f"{_BASE}/ncip").mock(return_value=Response(200, content=_SUCCESS_CHECKOUT))

    client = HttpNcipClient(_SETTINGS)
    try:
        result = await client.check_out(
            idempotency_key=_IDEM_KEY, item_id=_ITEM_ID, patron_id=_PATRON_ID
        )
    finally:
        await client.aclose()

    assert isinstance(result, NcipResult)
    assert result.state == "checked_out"
    assert result.item_id == _ITEM_ID
    assert result.patron_id == _PATRON_ID


# ---------------------------------------------------------------------------
# check_in
# ---------------------------------------------------------------------------


@respx.mock
async def test_checkin_sends_checkin_item_element() -> None:
    """check_in XML body contains a CheckInItem (no UserId — correct per spec)."""
    route = respx.post(f"{_BASE}/ncip").mock(
        return_value=Response(200, content=_SUCCESS_CHECKIN)
    )

    client = HttpNcipClient(_SETTINGS)
    try:
        await client.check_in(idempotency_key=_IDEM_KEY, item_id=_ITEM_ID)
    finally:
        await client.aclose()

    body = _parse_sent_xml(route.calls[0].request.content)
    checkin = body.find(f"{{{_NS}}}CheckInItem")
    assert checkin is not None, "CheckInItem element missing"
    # No UserId in CheckInItem (per wire samples)
    assert checkin.find(f"{{{_NS}}}UserId") is None
    item_val = checkin.findtext(
        f"{{{_NS}}}ItemId/{{{_NS}}}ItemIdentifierValue"
    )
    assert item_val == _ITEM_ID


@respx.mock
async def test_checkin_returns_ncip_result_with_empty_patron_id() -> None:
    """check_in returns NcipResult with state='checked_in' and patron_id=''."""
    respx.post(f"{_BASE}/ncip").mock(return_value=Response(200, content=_SUCCESS_CHECKIN))

    client = HttpNcipClient(_SETTINGS)
    try:
        result = await client.check_in(idempotency_key=_IDEM_KEY, item_id=_ITEM_ID)
    finally:
        await client.aclose()

    assert result.state == "checked_in"
    assert result.item_id == _ITEM_ID
    assert result.patron_id == ""


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@respx.mock
async def test_checkout_raises_ncip_error_on_500() -> None:
    """HTTP 500 + raw Problem XML raises NcipError (infrastructure failure)."""
    respx.post(f"{_BASE}/ncip").mock(
        return_value=Response(500, content=_INFRA_PROBLEM)
    )

    client = HttpNcipClient(_SETTINGS)
    try:
        raised = False
        try:
            await client.check_out(
                idempotency_key=_IDEM_KEY, item_id=_ITEM_ID, patron_id=_PATRON_ID
            )
        except NcipError as exc:
            raised = True
            assert "500" in str(exc)
            assert "problem processing NCIP request" in str(exc)
    finally:
        await client.aclose()

    assert raised, "NcipError not raised on HTTP 500"


@respx.mock
async def test_checkout_raises_ncip_error_on_application_problem() -> None:
    """HTTP 200 + Problem element in NCIPMessage raises NcipError (app failure)."""
    respx.post(f"{_BASE}/ncip").mock(
        return_value=Response(200, content=_APP_PROBLEM)
    )

    client = HttpNcipClient(_SETTINGS)
    try:
        raised = False
        try:
            await client.check_out(
                idempotency_key=_IDEM_KEY, item_id=_ITEM_ID, patron_id=_PATRON_ID
            )
        except NcipError as exc:
            raised = True
            assert "ITEM_NOT_CHECKED_OUT" in str(exc)
    finally:
        await client.aclose()

    assert raised, "NcipError not raised on application Problem"


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_returns_true_on_200() -> None:
    """health() GETs /admin/health and returns True when status is 200."""
    respx.get(f"{_BASE}/admin/health").mock(return_value=Response(200))

    client = HttpNcipClient(_SETTINGS)
    try:
        result = await client.health()
    finally:
        await client.aclose()

    assert result is True


@respx.mock
async def test_health_returns_false_on_503() -> None:
    """health() returns False when the health endpoint returns non-200."""
    respx.get(f"{_BASE}/admin/health").mock(return_value=Response(503))

    client = HttpNcipClient(_SETTINGS)
    try:
        result = await client.health()
    finally:
        await client.aclose()

    assert result is False
