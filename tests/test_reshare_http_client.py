"""Unit tests for HttpReShareClient, _parse, MockReShareClient, and get_client.

Uses ``respx`` to intercept httpx at the transport layer — no network calls.
Covers error paths in _post (404 / 4xx / 5xx / ConnectError), all five
public action methods, health(), and the factory function.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from agora.clients.errors import ClientError, NotFoundError, RemoteUnavailableError
from agora.clients.reshare import (
    ConnectionFailedError,
    HttpReShareClient,
    MockReShareClient,
    ReShareSendResult,
    _parse,
    get_client,
)
from agora.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_URL = "http://reshare.test"


def _settings(**overrides: Any) -> Settings:
    """Build a minimal Settings with reshare configured."""
    defaults: dict[str, Any] = {
        "RESHARE_BASE_URL": _BASE_URL,
        "RESHARE_TENANT": "test-tenant",
        "RESHARE_USER": "",
        "RESHARE_PASSWORD": "",
        "OKAPI_URL": "",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _client() -> HttpReShareClient:
    return HttpReShareClient(_settings())


_RESHARE_OK = {
    "id": "abc-123",
    "hrid": "REQ-001",
    "state": {"code": "Requested"},
}

_PERFORM_OK = {
    "id": "abc-123",
    "state": {"code": "Cancelled"},
}


# ---------------------------------------------------------------------------
# Constructor guard (line 174)
# ---------------------------------------------------------------------------


def test_http_reshare_client_raises_without_base_url() -> None:
    """Empty RESHARE_BASE_URL raises ClientError (line 174)."""
    with pytest.raises(ClientError, match="RESHARE_BASE_URL not configured"):
        HttpReShareClient(_settings(RESHARE_BASE_URL=""))


# ---------------------------------------------------------------------------
# _post error paths (lines 226-227, 230, 232, 234)
# ---------------------------------------------------------------------------


@respx.mock
async def test_post_raises_not_found_on_404() -> None:
    """_post raises NotFoundError on 404 response (line 230)."""
    respx.post(f"{_BASE_URL}/rs/patronrequests").mock(return_value=Response(404))
    client = _client()
    try:
        with pytest.raises(NotFoundError, match="404"):
            await client.send_request(
                idempotency_key="k1",
                request_payload={"title": "T"},
                supplier_symbol="LIB-A",
            )
    finally:
        await client.aclose()


@respx.mock
async def test_post_raises_client_error_on_4xx() -> None:
    """_post raises ClientError on 4xx (non-404) response (line 234)."""
    respx.post(f"{_BASE_URL}/rs/patronrequests").mock(return_value=Response(422, text="bad"))
    client = _client()
    try:
        with pytest.raises(ClientError, match="422"):
            await client.send_request(
                idempotency_key="k2",
                request_payload={"title": "T"},
                supplier_symbol="LIB-A",
            )
    finally:
        await client.aclose()


@respx.mock
async def test_post_4xx_redacts_response_body_in_error() -> None:
    """Audit #7: 4xx body must NOT appear verbatim in the ClientError message.

    mod-rs error responses can include patron identifiers, the inbound
    request echoed back, and occasionally auth-header hints. The
    exception's ``str()`` lands in ``outbox.last_error`` (a String(2048)
    column readable by anyone with API access), so the raw body must be
    truncated AND redacted before raising. The full body is logged at
    DEBUG for operator-side diagnosis.
    """
    leaky_body = (
        "PATRON_ID=alice@example.org "
        "REQUEST_BODY={'patron': {'patron_id': 'alice@example.org'}} "
        "Authorization: Bearer eyJsecrettoken12345"
        + ("X" * 2000)  # ensure body exceeds the 200-char snippet cap
    )
    respx.post(f"{_BASE_URL}/rs/patronrequests").mock(
        return_value=Response(403, text=leaky_body)
    )
    client = _client()
    try:
        with pytest.raises(ClientError) as excinfo:
            await client.send_request(
                idempotency_key="k-redact",
                request_payload={"title": "T"},
                supplier_symbol="LIB-A",
            )
    finally:
        await client.aclose()

    msg = str(excinfo.value)
    # Status code is preserved — operators need it for triage.
    assert "403" in msg
    # The verbatim body MUST be truncated. Snippet cap is 200, total
    # message stays well under outbox.last_error's 2048 limit.
    assert len(msg) < 600
    # The raw 2000-X tail must NOT appear in the truncated snippet.
    assert "X" * 1000 not in msg


@respx.mock
async def test_post_raises_remote_unavailable_on_5xx_without_retry() -> None:
    """_post raises RemoteUnavailableError on 5xx after exactly ONE attempt.

    A 5xx means the request reached mod-rs and MAY have been processed;
    mod-rs ignores Idempotency-Key, so an automatic retry could create
    a duplicate supplier-side PatronRequest. Replay belongs to the
    outbox worker (staff-visible), not the transport layer.
    """
    route = respx.post(f"{_BASE_URL}/rs/patronrequests").mock(return_value=Response(503))
    client = _client()
    try:
        with pytest.raises(RemoteUnavailableError, match="503"):
            await client.send_request(
                idempotency_key="k3",
                request_payload={"title": "T"},
                supplier_symbol="LIB-A",
            )
    finally:
        await client.aclose()
    assert route.call_count == 1


@respx.mock
async def test_post_read_timeout_single_attempt_no_retry() -> None:
    """ReadTimeout: the request was sent and MAY have been processed —
    exactly one attempt, surfaced as RemoteUnavailableError (NOT the
    retryable ConnectionFailedError subclass)."""
    route = respx.post(f"{_BASE_URL}/rs/patronrequests").mock(
        side_effect=httpx.ReadTimeout("read timed out")
    )
    client = _client()
    try:
        with pytest.raises(RemoteUnavailableError) as excinfo:
            await client.send_request(
                idempotency_key="k-rt",
                request_payload={"title": "T"},
                supplier_symbol="LIB-A",
            )
    finally:
        await client.aclose()
    assert route.call_count == 1
    assert not isinstance(excinfo.value, ConnectionFailedError)


@respx.mock
async def test_post_retries_connect_error() -> None:
    """ConnectError (connection never established — request provably
    never reached the server) IS retried: 3 attempts, then
    ConnectionFailedError (a RemoteUnavailableError subclass, so the
    caller-facing contract is unchanged)."""
    route = respx.post(f"{_BASE_URL}/rs/patronrequests").mock(
        side_effect=httpx.ConnectError("refused")
    )
    client = _client()
    try:
        with pytest.raises(ConnectionFailedError):
            await client.send_request(
                idempotency_key="k4",
                request_payload={"title": "T"},
                supplier_symbol="LIB-A",
            )
    finally:
        await client.aclose()
    assert route.call_count == 3


# ---------------------------------------------------------------------------
# Public action methods (lines 270-308)
# ---------------------------------------------------------------------------


@respx.mock
async def test_send_request_posts_correct_body() -> None:
    """send_request merges supplier symbol into body and returns parsed result (lines 270-274)."""
    route = respx.post(f"{_BASE_URL}/rs/patronrequests").mock(
        return_value=Response(200, json=_RESHARE_OK)
    )
    client = _client()
    try:
        result = await client.send_request(
            idempotency_key="idem-1",
            request_payload={"title": "Brave New World"},
            supplier_symbol="LIB-X",
        )
    finally:
        await client.aclose()

    assert route.called
    body = route.calls.last.request.read()
    import json

    parsed_body = json.loads(body)
    assert parsed_body["supplyingInstitutionSymbol"] == "LIB-X"
    assert parsed_body["title"] == "Brave New World"
    assert isinstance(result, ReShareSendResult)
    assert result.reshare_id == "abc-123"
    assert result.state == "Requested"


@respx.mock
async def test_cancel_request_posts_perform_action() -> None:
    """cancel_request calls performAction with requesterCancel (lines 279-285)."""
    reshare_id = "abc-123"
    route = respx.post(
        f"{_BASE_URL}/rs/patronrequests/{reshare_id}/performAction"
    ).mock(return_value=Response(200, json=_PERFORM_OK))
    client = _client()
    try:
        result = await client.cancel_request(
            idempotency_key="idem-cancel",
            reshare_id=reshare_id,
            reason="patron no longer needs",
        )
    finally:
        await client.aclose()

    assert route.called
    assert isinstance(result, ReShareSendResult)
    assert result.state == "Cancelled"


@respx.mock
async def test_confirm_shipment_posts_perform_action() -> None:
    """confirm_shipment calls performAction with supplierMarkShipped (lines 290-295)."""
    reshare_id = "abc-123"
    respx.post(
        f"{_BASE_URL}/rs/patronrequests/{reshare_id}/performAction"
    ).mock(return_value=Response(200, json={"id": reshare_id, "state": {"code": "Loaned"}}))
    client = _client()
    try:
        result = await client.confirm_shipment(
            idempotency_key="idem-ship",
            reshare_id=reshare_id,
        )
    finally:
        await client.aclose()

    assert result.state == "Loaned"


@respx.mock
async def test_confirm_return_posts_perform_action() -> None:
    """confirm_return calls performAction with patronReturnedItem (lines 303-308)."""
    reshare_id = "abc-123"
    respx.post(
        f"{_BASE_URL}/rs/patronrequests/{reshare_id}/performAction"
    ).mock(return_value=Response(200, json={"id": reshare_id, "state": {"code": "LoanCompleted"}}))
    client = _client()
    try:
        result = await client.confirm_return(
            idempotency_key="idem-ret",
            reshare_id=reshare_id,
        )
    finally:
        await client.aclose()

    assert result.state == "LoanCompleted"


# ---------------------------------------------------------------------------
# health() (lines 334-341)
# ---------------------------------------------------------------------------


@respx.mock
async def test_health_returns_true_on_200() -> None:
    """health() returns True when patronrequests probe returns 200 (lines 334-341)."""
    respx.get(f"{_BASE_URL}/rs/patronrequests").mock(return_value=Response(200, json=[]))
    client = _client()
    try:
        ok = await client.health()
    finally:
        await client.aclose()
    assert ok is True


@respx.mock
async def test_health_returns_false_on_network_error() -> None:
    """health() returns False on ConnectError (line 339-340)."""
    respx.get(f"{_BASE_URL}/rs/patronrequests").mock(
        side_effect=httpx.ConnectError("refused")
    )
    client = _client()
    try:
        ok = await client.health()
    finally:
        await client.aclose()
    assert ok is False


# ---------------------------------------------------------------------------
# _parse — string state branch (line 362)
# ---------------------------------------------------------------------------


def test_parse_with_string_state() -> None:
    """_parse handles string state (not dict) via else branch (line 362)."""
    result = _parse({"id": "x1", "state": "Shipped"})
    assert result.state == "Shipped"
    assert result.reshare_id == "x1"


def test_parse_with_null_state_defaults_to_requested() -> None:
    """_parse falls back to 'Requested' when state is null (line 362)."""
    result = _parse({"id": "x2", "state": None})
    assert result.state == "Requested"


# ---------------------------------------------------------------------------
# MockReShareClient — unknown reshare_id raises NotFoundError (line 484)
# ---------------------------------------------------------------------------


async def test_mock_reshare_client_transition_unknown_id_raises() -> None:
    """_transition raises NotFoundError for unknown reshare_id (line 484)."""
    mock = MockReShareClient()
    with pytest.raises(NotFoundError, match="not found"):
        await mock.cancel_request(
            idempotency_key="k",
            reshare_id="nonexistent-id",
            reason="test",
        )


# ---------------------------------------------------------------------------
# MockReShareClient.health() (line 500)
# ---------------------------------------------------------------------------


async def test_mock_reshare_client_health_returns_true() -> None:
    """MockReShareClient.health() always returns True (line 500)."""
    mock = MockReShareClient()
    assert await mock.health() is True


# ---------------------------------------------------------------------------
# get_client() factory (line 507)
# ---------------------------------------------------------------------------


async def test_get_client_returns_http_client_when_configured() -> None:
    """get_client() returns HttpReShareClient when RESHARE_BASE_URL is set (line 507)."""
    with patch("agora.clients.reshare.get_settings", return_value=_settings()):
        client = get_client()
    assert isinstance(client, HttpReShareClient)
    await client.aclose()


# ---------------------------------------------------------------------------
# HttpReShareClient.renew_request — sandbox-blocked ClientError (line 322)
# ---------------------------------------------------------------------------


async def test_http_reshare_renew_request_raises_client_error() -> None:
    """renew_request is sandbox-blocked; HttpReShareClient raises ClientError
    immediately so the outbox worker surfaces a dead-letter row (ADR-0017)."""
    client = _client()
    try:
        with pytest.raises(ClientError, match="sandbox-blocked"):
            await client.renew_request(
                idempotency_key="idem-renew",
                reshare_id="some-reshare-id",
                extension_days=14,
            )
    finally:
        await client.aclose()
