"""Unit tests for HttpNcipClient, MockNcipClient, _parse_response, and get_client.

Uses ``respx`` to intercept httpx at the transport layer — no network calls.
Covers error paths in _parse_response (5xx / 4xx / bad-XML), HttpNcipClient
constructor guards, _post_ncip network error, health(), and the factory.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from agora.clients.errors import ClientError, RemoteUnavailableError
from agora.clients.ncip import (
    HttpNcipClient,
    MockNcipClient,
    NcipError,
    _parse_response,
    get_client,
)
from agora.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_URL = "http://ncip.test"
_AGENCY = "TEST-AGENCY"


def _settings(**overrides: Any) -> Settings:
    """Build a minimal Settings with NCIP configured."""
    defaults: dict[str, Any] = {
        "NCIP_BASE_URL": _BASE_URL,
        "NCIP_AGENCY_ID": _AGENCY,
        "RESHARE_TENANT": "test-tenant",
        "RESHARE_USER": "",
        "RESHARE_PASSWORD": "",
        "OKAPI_URL": "",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _client() -> HttpNcipClient:
    return HttpNcipClient(_settings())


# Minimal valid NCIP 200 response (no Problem element)
_NCIP_NS = "http://www.niso.org/2008/ncip"
_NCIP_OK_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<NCIPMessage xmlns="http://www.niso.org/2008/ncip">'
    b"<CheckOutItemResponse/>"
    b"</NCIPMessage>"
)


# ---------------------------------------------------------------------------
# MockNcipClient.health() — line 169
# ---------------------------------------------------------------------------


async def test_mock_ncip_client_health_returns_true() -> None:
    """MockNcipClient.health() always returns True (line 169)."""
    mock = MockNcipClient()
    assert await mock.health() is True


# ---------------------------------------------------------------------------
# _parse_response — 5xx with invalid XML (lines 288-289)
# ---------------------------------------------------------------------------


def test_parse_response_5xx_invalid_xml_uses_raw_text() -> None:
    """5xx with unparseable XML falls back to raw text (lines 288-289)."""
    bad_xml = b"<not valid xml"
    with pytest.raises(NcipError, match="NCIP infrastructure error 503"):
        _parse_response(503, bad_xml)


def test_parse_response_5xx_valid_xml_extracts_message() -> None:
    """5xx with valid Problem XML extracts message element (line 287)."""
    xml = b"<Problem><message>Service unavailable</message></Problem>"
    with pytest.raises(NcipError, match="Service unavailable"):
        _parse_response(503, xml)


# ---------------------------------------------------------------------------
# _parse_response — 4xx (line 293)
# ---------------------------------------------------------------------------


def test_parse_response_4xx_raises_ncip_http_error() -> None:
    """4xx raises NcipError with HTTP status (line 293)."""
    with pytest.raises(NcipError, match="NCIP HTTP error 422"):
        _parse_response(422, b"")


# ---------------------------------------------------------------------------
# _parse_response — 200 with invalid XML (lines 298-299)
# ---------------------------------------------------------------------------


def test_parse_response_200_invalid_xml_raises_ncip_error() -> None:
    """200 response with unparseable body raises NcipError (lines 298-299)."""
    with pytest.raises(NcipError, match="NCIP response parse error"):
        _parse_response(200, b"<not valid xml")


# ---------------------------------------------------------------------------
# HttpNcipClient constructor — line 327
# ---------------------------------------------------------------------------


def test_http_ncip_client_raises_without_base_url() -> None:
    """Empty NCIP_BASE_URL raises ClientError (line 327)."""
    with pytest.raises(ClientError, match="NCIP_BASE_URL not configured"):
        HttpNcipClient(_settings(NCIP_BASE_URL=""))


# ---------------------------------------------------------------------------
# HttpNcipClient constructor — OkapiAuth wired (line 333)
# ---------------------------------------------------------------------------


def test_http_ncip_client_wires_okapi_auth_when_okapi_url_set() -> None:
    """OkapiAuth is attached when OKAPI_URL is configured (line 333)."""
    client = HttpNcipClient(
        _settings(OKAPI_URL="http://okapi.test", RESHARE_USER="u", RESHARE_PASSWORD="p")
    )
    # The auth object is set on the underlying httpx client
    assert client._client.auth is not None


# ---------------------------------------------------------------------------
# _post_ncip — network error (lines 366-367)
# ---------------------------------------------------------------------------


@respx.mock
async def test_post_ncip_raises_remote_unavailable_on_request_error() -> None:
    """_post_ncip raises RemoteUnavailableError on httpx.RequestError (lines 366-367)."""
    respx.post(f"{_BASE_URL}/ncip").mock(side_effect=httpx.ConnectError("refused"))
    client = _client()
    try:
        with pytest.raises(RemoteUnavailableError):
            await client.check_out(
                idempotency_key="k1",
                item_id="item-001",
                patron_id="patron-001",
            )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# check_out — happy path (exercises _post_ncip + return value)
# ---------------------------------------------------------------------------


@respx.mock
async def test_check_out_returns_ncip_result_on_success() -> None:
    """check_out returns NcipResult with state='checked_out' on 200 OK."""
    respx.post(f"{_BASE_URL}/ncip").mock(return_value=Response(200, content=_NCIP_OK_XML))
    client = _client()
    try:
        result = await client.check_out(
            idempotency_key="k2",
            item_id="item-001",
            patron_id="patron-001",
        )
    finally:
        await client.aclose()

    assert result.state == "checked_out"
    assert result.item_id == "item-001"
    assert result.patron_id == "patron-001"


# ---------------------------------------------------------------------------
# health() — lines 396-397
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_returns_true_on_200() -> None:
    """health() returns True when admin/health probe returns 200."""
    respx.get(f"{_BASE_URL}/admin/health").mock(return_value=Response(200))
    client = _client()
    try:
        ok = await client.health()
    finally:
        await client.aclose()
    assert ok is True


@respx.mock
async def test_health_returns_false_on_request_error() -> None:
    """health() returns False on httpx.RequestError (lines 396-397)."""
    respx.get(f"{_BASE_URL}/admin/health").mock(
        side_effect=httpx.ConnectError("refused")
    )
    client = _client()
    try:
        ok = await client.health()
    finally:
        await client.aclose()
    assert ok is False


# ---------------------------------------------------------------------------
# get_client() factory — lines 409-410
# ---------------------------------------------------------------------------


def test_get_client_returns_http_client_when_configured() -> None:
    """get_client() returns HttpNcipClient when NCIP_BASE_URL is set (lines 409-410)."""
    with patch("agora.clients.ncip.get_settings", return_value=_settings()):
        client = get_client()
    assert isinstance(client, HttpNcipClient)


def test_get_client_returns_mock_when_not_configured() -> None:
    """get_client() returns MockNcipClient when NCIP_BASE_URL is empty."""
    with patch("agora.clients.ncip.get_settings", return_value=_settings(NCIP_BASE_URL="")):
        client = get_client()
    assert isinstance(client, MockNcipClient)
