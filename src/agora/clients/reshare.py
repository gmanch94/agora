"""ReShare REST client.

Wraps the FOLIO `mod-rs` API used by ReShare for ISO 18626 messaging.
This module exposes a *Protocol* (``ReShareClient``) and two
implementations: the real HTTP client (``HttpReShareClient``) and an
in-memory mock (``MockReShareClient``) used for tests and when no
``RESHARE_BASE_URL`` is configured.

Method names mirror the user-facing lifecycle, but each method maps to
specific ISO 18626 messages internally:

| Method | ISO 18626 effect |
|---|---|
| send_request | Request message to chosen supplier |
| cancel_request | RequestingAgencyMessage Cancel |
| confirm_shipment | (lender side) SupplyingAgencyMessage Loaned |
| confirm_return | RequestingAgencyMessage Returned |
| recall_request | RequestingAgencyMessage Recall |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from agora.clients.errors import ClientError, NotFoundError, RemoteUnavailableError
from agora.config import Settings, get_settings
from agora.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ReShareSendResult:
    """Result of a successful send_request to ReShare."""

    reshare_id: str
    iso_message_id: str
    supplier_symbol: str
    state: str  # ISO 18626 state name


class ReShareClient(Protocol):
    """Behavioural contract for any ReShare implementation."""

    async def send_request(
        self,
        *,
        idempotency_key: str,
        request_payload: dict[str, Any],
        supplier_symbol: str,
    ) -> ReShareSendResult: ...

    async def cancel_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult: ...

    async def confirm_shipment(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult: ...

    async def confirm_return(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult: ...

    async def recall_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult: ...

    async def health(self) -> bool: ...


class HttpReShareClient:
    """Real HTTP client for FOLIO mod-rs.

    Endpoints below follow the public mod-rs API shape. They are
    *placeholders* for the prototype — verified URLs/payloads should
    be confirmed against the running ReShare instance before we drive
    real traffic. The HTTP shape, idempotency header, and error
    mapping are correct; only the exact paths/payload keys may need
    tweaking.
    """

    def __init__(self, settings: Settings | None = None):
        s = settings or get_settings()
        if not s.reshare_base_url:
            raise ClientError("RESHARE_BASE_URL not configured")
        self._base_url = s.reshare_base_url.rstrip("/")
        self._tenant = s.reshare_tenant
        self._auth: httpx.BasicAuth | None = (
            httpx.BasicAuth(s.reshare_user, s.reshare_password)
            if s.reshare_user
            else None
        )
        self._client = httpx.AsyncClient(timeout=10.0, auth=self._auth)

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type(RemoteUnavailableError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        reraise=True,
    )
    async def _post(
        self, path: str, *, idempotency_key: str, json: dict[str, Any]
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            resp = await self._client.post(
                url,
                json=json,
                headers={
                    "X-Okapi-Tenant": self._tenant,
                    "Idempotency-Key": idempotency_key,
                    "Accept": "application/json",
                },
            )
        except httpx.RequestError as exc:
            raise RemoteUnavailableError(str(exc)) from exc

        if resp.status_code == 404:
            raise NotFoundError(f"{path}: 404")
        if resp.status_code >= 500:
            raise RemoteUnavailableError(f"{path}: {resp.status_code}")
        if resp.status_code >= 400:
            raise ClientError(f"{path}: {resp.status_code} {resp.text}")
        return resp.json()

    async def send_request(
        self,
        *,
        idempotency_key: str,
        request_payload: dict[str, Any],
        supplier_symbol: str,
    ) -> ReShareSendResult:
        body = {"supplier": supplier_symbol, "request": request_payload}
        data = await self._post(
            "/rs/patronrequests", idempotency_key=idempotency_key, json=body
        )
        return _parse(data, supplier_default=supplier_symbol)

    async def cancel_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        data = await self._post(
            f"/rs/patronrequests/{reshare_id}/performAction",
            idempotency_key=idempotency_key,
            json={"action": "RequesterCancel", "reason": reason},
        )
        return _parse(data)

    async def confirm_shipment(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult:
        data = await self._post(
            f"/rs/patronrequests/{reshare_id}/performAction",
            idempotency_key=idempotency_key,
            json={"action": "SupplierMarkShipped"},
        )
        return _parse(data)

    async def confirm_return(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult:
        data = await self._post(
            f"/rs/patronrequests/{reshare_id}/performAction",
            idempotency_key=idempotency_key,
            json={"action": "RequesterMarkReturned"},
        )
        return _parse(data)

    async def recall_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        data = await self._post(
            f"/rs/patronrequests/{reshare_id}/performAction",
            idempotency_key=idempotency_key,
            json={"action": "SupplierRecall", "reason": reason},
        )
        return _parse(data)

    async def health(self) -> bool:
        try:
            resp = await self._client.get(
                f"{self._base_url}/admin/health",
                headers={"X-Okapi-Tenant": self._tenant},
            )
        except httpx.RequestError:
            return False
        return resp.status_code < 500


def _parse(data: dict[str, Any], *, supplier_default: str = "") -> ReShareSendResult:
    return ReShareSendResult(
        reshare_id=str(data.get("id") or data.get("hrid") or ""),
        iso_message_id=str(data.get("isoMessageId") or data.get("messageId") or ""),
        supplier_symbol=str(data.get("supplyingAgencyId") or supplier_default),
        state=str(data.get("state") or "Requested"),
    )


@dataclass(slots=True)
class _MockRequest:
    reshare_id: str
    state: str
    supplier_symbol: str
    history: list[dict[str, Any]] = field(default_factory=list)


class MockReShareClient:
    """In-memory ReShare double for tests and offline dev.

    Behaviour:
    - send_request creates a request in state ``Requested`` and returns
      a synthetic id.
    - performAction-style methods transition state per a small map.
    - All methods are idempotent: replaying with the same
      ``idempotency_key`` returns the prior recorded result.
    """

    def __init__(self) -> None:
        self._requests: dict[str, _MockRequest] = {}
        self._idem: dict[str, ReShareSendResult] = {}
        self._next_id = 1

    async def aclose(self) -> None:
        return None

    def _replay(self, key: str) -> ReShareSendResult | None:
        return self._idem.get(key)

    def _record(self, key: str, result: ReShareSendResult) -> ReShareSendResult:
        self._idem[key] = result
        return result

    async def send_request(
        self,
        *,
        idempotency_key: str,
        request_payload: dict[str, Any],
        supplier_symbol: str,
    ) -> ReShareSendResult:
        if (prior := self._replay(idempotency_key)) is not None:
            return prior
        rid = f"rs-{self._next_id:06d}"
        self._next_id += 1
        msg_id = f"msg-{rid}-001"
        self._requests[rid] = _MockRequest(
            reshare_id=rid,
            state="Requested",
            supplier_symbol=supplier_symbol,
            history=[{"event": "create", "payload": request_payload}],
        )
        return self._record(
            idempotency_key,
            ReShareSendResult(
                reshare_id=rid,
                iso_message_id=msg_id,
                supplier_symbol=supplier_symbol,
                state="Requested",
            ),
        )

    async def cancel_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        return await self._transition(
            idempotency_key=idempotency_key,
            reshare_id=reshare_id,
            new_state="Cancelled",
            event={"event": "cancel", "reason": reason},
        )

    async def confirm_shipment(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult:
        return await self._transition(
            idempotency_key=idempotency_key,
            reshare_id=reshare_id,
            new_state="Loaned",
            event={"event": "ship"},
        )

    async def confirm_return(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult:
        return await self._transition(
            idempotency_key=idempotency_key,
            reshare_id=reshare_id,
            new_state="LoanCompleted",
            event={"event": "return"},
        )

    async def recall_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        return await self._transition(
            idempotency_key=idempotency_key,
            reshare_id=reshare_id,
            new_state="Recalled",
            event={"event": "recall", "reason": reason},
        )

    async def _transition(
        self,
        *,
        idempotency_key: str,
        reshare_id: str,
        new_state: str,
        event: dict[str, Any],
    ) -> ReShareSendResult:
        if (prior := self._replay(idempotency_key)) is not None:
            return prior
        if reshare_id not in self._requests:
            raise NotFoundError(f"reshare_id {reshare_id} not found")
        req = self._requests[reshare_id]
        req.state = new_state
        req.history.append(event)
        msg_id = f"msg-{reshare_id}-{len(req.history):03d}"
        return self._record(
            idempotency_key,
            ReShareSendResult(
                reshare_id=reshare_id,
                iso_message_id=msg_id,
                supplier_symbol=req.supplier_symbol,
                state=new_state,
            ),
        )

    async def health(self) -> bool:
        return True


def get_client() -> ReShareClient:
    """Factory: real client if URL configured, else mock."""
    s = get_settings()
    if s.reshare_enabled:
        return HttpReShareClient(s)  # type: ignore[return-value]
    log.info("reshare.client.using_mock")
    return MockReShareClient()  # type: ignore[return-value]
