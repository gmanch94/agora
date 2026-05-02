"""ReShare REST client.

Wraps the FOLIO `mod-rs` API used by ReShare for ISO 18626 messaging.
This module exposes a *Protocol* (``ReShareClient``) and two
implementations: the real HTTP client (``HttpReShareClient``) and an
in-memory mock (``MockReShareClient``) used for tests and when no
``RESHARE_BASE_URL`` is configured.

Method names mirror the user-facing lifecycle, but each method maps to
specific ISO 18626 messages internally:

| Method | ISO 18626 effect | mod-rs action |
|---|---|---|
| send_request | Request message to chosen supplier | (POST /rs/patronrequests create) |
| cancel_request | RequestingAgencyMessage Cancel | ``requesterCancel`` |
| confirm_shipment | (lender side) SupplyingAgencyMessage Loaned | ``supplierMarkShipped`` |
| confirm_return | RequestingAgencyMessage Returned | ``patronReturnedItem`` (borrower side) |
| recall_request | RequestingAgencyMessage Recall | *(no first-class action — see method)* |

**API verification (2026-05-02).** Endpoint paths, action vocabulary,
request/response shapes verified against mod-rs master:

- ``service/grails-app/controllers/mod/rs/UrlMappings.groovy``
- ``service/grails-app/controllers/mod/rs/PatronRequestController.groovy``
- ``service/src/main/groovy/org/olf/rs/statemodel/Actions.groovy``
- ``service/src/main/okapi/ModuleDescriptor-template.json``

Notes from that probe:

1. mod-rs does **not** honour an ``Idempotency-Key`` header. We send it
   anyway (Okapi forwards unknown headers harmlessly); replay-safety
   comes from the saga ledger's ``saga_event.idempotency_key`` UNIQUE
   constraint, not the wire.
2. ``performAction`` body is ``{action, actionParams}`` (camelCase
   action), **not** ``{action, reason}``. Reasons go inside
   ``actionParams``.
3. mod-rs does not declare ``/admin/health``; we probe with a cheap
   ``GET /rs/patronrequests?perPage=0`` instead.
4. Auth in production is the Okapi token flow (``X-Okapi-Token`` from
   ``POST /authn/login``). HTTP Basic against the module's direct port
   works only when permissions are disabled — kept here as a dev path.
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

    Paths and action strings verified against mod-rs master (see module
    docstring for source files). The remaining unverified surface is
    the **create-request body shape** — mod-rs binds the POST body
    directly onto its ``PatronRequest`` Grails domain object, so the
    exact field names depend on that class. We pass the caller's
    ``request_payload`` through as the top-level body and merge the
    chosen supplier under ``supplyingInstitutionSymbol``; if the
    domain class uses different keys, callers must shape
    ``request_payload`` accordingly.

    Auth: HTTP Basic is dev-only (works when hitting the module's
    direct port with permissions disabled). Real deployments use the
    Okapi token flow — wire that here once we have a live tenant.
    """

    # Action strings honoured by mod-rs's PatronRequestController.
    # Source: service/src/main/groovy/org/olf/rs/statemodel/Actions.groovy
    _ACTION_REQUESTER_CANCEL = "requesterCancel"
    _ACTION_SUPPLIER_MARK_SHIPPED = "supplierMarkShipped"
    _ACTION_PATRON_RETURNED_ITEM = "patronReturnedItem"

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

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        h = {
            "X-Okapi-Tenant": self._tenant,
            "Accept": "application/json",
        }
        if idempotency_key is not None:
            # mod-rs ignores this header — kept for upstream proxies and
            # log-correlation. Real dedup lives in the saga ledger.
            h["Idempotency-Key"] = idempotency_key
        return h

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
                url, json=json, headers=self._headers(idempotency_key=idempotency_key)
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

    async def _perform_action(
        self,
        *,
        reshare_id: str,
        idempotency_key: str,
        action: str,
        action_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /rs/patronrequests/{id}/performAction with verified body shape.

        Body is ``{"action": <camelCase>, "actionParams": {...}}`` per
        ``PatronRequestController.performAction`` — *not* a flat
        ``{action, reason}`` envelope.
        """
        return await self._post(
            f"/rs/patronrequests/{reshare_id}/performAction",
            idempotency_key=idempotency_key,
            json={"action": action, "actionParams": action_params or {}},
        )

    async def send_request(
        self,
        *,
        idempotency_key: str,
        request_payload: dict[str, Any],
        supplier_symbol: str,
    ) -> ReShareSendResult:
        # mod-rs deserialises this body straight onto its PatronRequest
        # domain object. Top-level fields (title, author, patronIdentifier,
        # requestingInstitutionSymbol, supplyingInstitutionSymbol, ...)
        # must match the domain class — caller's request_payload is
        # passed through verbatim, with the chosen supplier merged in.
        body = {**request_payload, "supplyingInstitutionSymbol": supplier_symbol}
        data = await self._post(
            "/rs/patronrequests", idempotency_key=idempotency_key, json=body
        )
        return _parse(data, supplier_default=supplier_symbol)

    async def cancel_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        data = await self._perform_action(
            reshare_id=reshare_id,
            idempotency_key=idempotency_key,
            action=self._ACTION_REQUESTER_CANCEL,
            action_params={"reason": reason},
        )
        return _parse(data)

    async def confirm_shipment(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult:
        data = await self._perform_action(
            reshare_id=reshare_id,
            idempotency_key=idempotency_key,
            action=self._ACTION_SUPPLIER_MARK_SHIPPED,
        )
        return _parse(data)

    async def confirm_return(
        self, *, idempotency_key: str, reshare_id: str
    ) -> ReShareSendResult:
        # Borrower-side: the requester confirms the patron handed the
        # item back. Lender-side equivalent (``itemReturned``) belongs
        # in a separate code path if/when we drive supplier flows.
        data = await self._perform_action(
            reshare_id=reshare_id,
            idempotency_key=idempotency_key,
            action=self._ACTION_PATRON_RETURNED_ITEM,
        )
        return _parse(data)

    async def recall_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        # mod-rs's Actions.groovy has no first-class "recall" action.
        # ISO 18626 recall is a RequestingAgencyMessage; the right
        # mod-rs mapping (probably ``message`` with a recall reason
        # body, or a state-specific action we haven't found) needs
        # confirmation against a live ReShare instance before we drive
        # real traffic. Fail loudly rather than silently 4xx.
        raise ClientError(
            "recall_request: mod-rs action mapping unverified — "
            "needs confirmation against running ReShare. "
            f"reshare_id={reshare_id} reason={reason!r}"
        )

    async def health(self) -> bool:
        # mod-rs does not declare /admin/health. Use a cheap
        # collection probe instead — declared in ModuleDescriptor with
        # permission ``rs.patronrequests.collection.get``.
        try:
            resp = await self._client.get(
                f"{self._base_url}/rs/patronrequests?perPage=0",
                headers=self._headers(),
            )
        except httpx.RequestError:
            return False
        return resp.status_code in (200, 204)


def _parse(data: dict[str, Any], *, supplier_default: str = "") -> ReShareSendResult:
    """Best-effort ReShare response parse.

    mod-rs returns Grails-marshalled JSON. ``state`` is a refdata
    association (``{"code": "...", "label": "..."}``) — we flatten to
    its ``code``. ``isoMessageId`` / ``supplyingAgencyId`` are not
    standard top-level fields on the create response; until we have
    sample payloads from a live tenant they may come back empty.
    """
    state = data.get("state")
    if isinstance(state, dict):
        state_str = str(state.get("code") or state.get("label") or "Requested")
    else:
        state_str = str(state or "Requested")
    return ReShareSendResult(
        reshare_id=str(data.get("id") or data.get("hrid") or ""),
        iso_message_id=str(data.get("isoMessageId") or data.get("messageId") or ""),
        supplier_symbol=str(data.get("supplyingAgencyId") or supplier_default),
        state=state_str,
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
