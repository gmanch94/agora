"""Unit tests for HttpReShareClient.recall_request — ADR-0016.

Verifies that recall_request dispatches ``manualClose`` via
performAction with the reason string in ``actionParams``, and that
_parse correctly surfaces the response fields.

Uses ``respx`` to mock httpx at the transport layer without spinning
up a real server.
"""

from __future__ import annotations

import json as _json

import respx
from httpx import Response

from agora.clients.reshare import HttpReShareClient, ReShareSendResult
from agora.config import Settings

_RESHARE_ID = "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"
_IDEM_KEY = "ship-comp-test-001"
_REASON = "ship-step compensator: recall"
_BASE = "http://mod-rs.test"
_TENANT = "diku"

_SETTINGS = Settings(
    RESHARE_BASE_URL=_BASE,
    RESHARE_TENANT=_TENANT,
    RESHARE_USER="",
    RESHARE_PASSWORD="",
    OKAPI_URL="",
)

# Minimal mod-rs performAction response (manualClose returns the updated record).
_MANUAL_CLOSE_RESPONSE = {
    "id": _RESHARE_ID,
    "state": {"code": "REQ_CANCELLED", "label": "Cancelled"},
    "requestingInstitutionSymbol": "CONSORTIUM-A",
}


@respx.mock
async def test_recall_request_dispatches_manualclose() -> None:
    """recall_request POSTs manualClose to performAction with reason in actionParams."""
    route = respx.post(
        f"{_BASE}/rs/patronrequests/{_RESHARE_ID}/performAction"
    ).mock(return_value=Response(200, json=_MANUAL_CLOSE_RESPONSE))

    client = HttpReShareClient(_SETTINGS)
    try:
        result = await client.recall_request(
            idempotency_key=_IDEM_KEY,
            reshare_id=_RESHARE_ID,
            reason=_REASON,
        )
    finally:
        await client.aclose()

    # Exactly one call made
    assert route.called
    assert route.call_count == 1

    # Verify request body: action must be manualClose, reason in actionParams
    sent = route.calls[0].request
    body = _json.loads(sent.content)
    assert body["action"] == "manualClose", (
        f"expected manualClose, got {body['action']!r}"
    )
    assert body["actionParams"]["reason"] == _REASON

    # Verify tenant header
    assert sent.headers["X-Okapi-Tenant"] == _TENANT

    # Verify parsed result
    assert isinstance(result, ReShareSendResult)
    assert result.reshare_id == _RESHARE_ID
    assert result.state == "REQ_CANCELLED"


def test_recall_request_uses_action_constant() -> None:
    """_ACTION_MANUAL_CLOSE constant equals 'manualClose'."""
    assert HttpReShareClient._ACTION_MANUAL_CLOSE == "manualClose"


@respx.mock
async def test_recall_request_includes_idempotency_key_header() -> None:
    """Idempotency-Key header is forwarded (log-correlation; mod-rs ignores it)."""
    respx.post(
        f"{_BASE}/rs/patronrequests/{_RESHARE_ID}/performAction"
    ).mock(return_value=Response(200, json=_MANUAL_CLOSE_RESPONSE))

    client = HttpReShareClient(_SETTINGS)
    try:
        await client.recall_request(
            idempotency_key=_IDEM_KEY,
            reshare_id=_RESHARE_ID,
            reason=_REASON,
        )
    finally:
        await client.aclose()

    sent = respx.calls[0].request
    assert sent.headers.get("Idempotency-Key") == _IDEM_KEY
