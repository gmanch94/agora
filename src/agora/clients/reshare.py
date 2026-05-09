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
| recall_request | RequestingAgencyMessage Recall | ``manualClose`` (force-close — ADR-0016) |

**API verification (2026-05-02).** Endpoint paths, action vocabulary,
request/response shapes verified against mod-rs master:

- ``service/grails-app/controllers/mod/rs/UrlMappings.groovy``
- ``service/grails-app/controllers/mod/rs/PatronRequestController.groovy``
- ``service/src/main/groovy/org/olf/rs/statemodel/Actions.groovy``
- ``service/src/main/okapi/ModuleDescriptor-template.json``

Notes from static source review (2026-05-02) and local sandbox probe
(2026-05-06, ``make reshare-probe`` against
``ghcr.io/openlibraryenvironment/mod-rs:2.19.0-rc17``):

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
5. **POST /rs/patronrequests body shape (probe-confirmed).**
   camelCase fields ``title``, ``author``, ``isbn``,
   ``requestingInstitutionSymbol``, ``supplyingInstitutionSymbol``,
   ``patronIdentifier``, ``patronType``, ``pickupLocation``,
   ``neededBy`` are all accepted and stored by mod-rs. Passing
   ``supplyingInstitutionSymbol`` caused mod-rs to create the record
   under the *Responder* state model (``RES_IDLE``); the Requester
   state model (``REQ_*``) applies when the borrowing tenant creates the
   request without a pre-set supplier — Requester-side creation via
   direct API is still unconfirmed against a real borrower-tenant.
6. **Response field names (probe-confirmed).**
   ``id`` carries the UUID reshare_id. ``hrid`` may be absent/null.
   ``state`` is a refdata dict; its ``code`` key is the state string.
   ``isoMessageId`` and ``supplyingAgencyId`` are **not** present on
   basic create/GET responses — they appear only in ISO 18626
   protocol-level contexts (if at all).
7. **No requester-initiated recall action exists (probe-confirmed).**
   ``Actions.groovy`` defines no ``recall``, ``requesterRecall``, or
   ``borrowerRecall`` action. ``REQ_RECALLED`` is a *destination* state
   the supplier drives; it is not reachable by the requester via
   ``performAction``. From ``REQ_SHIPPED``, only ``requesterReceived``
   is a manual action; all others are inbound ISO 18626 protocol
   triggers. ADR-0016 (2026-05-06) resolved this: ``recall_request``
   calls ``manualClose`` (force-close; no supplier notification) as a
   prototype expedient. Option A (ISO 18626 Cancel via ``message``)
   is the production path — see ADR-0016 for trade-offs.
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
from agora.clients.okapi_auth import OkapiAuth
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

    async def renew_request(
        self, *, idempotency_key: str, reshare_id: str, extension_days: int
    ) -> ReShareSendResult: ...

    async def health(self) -> bool: ...

    async def aclose(self) -> None:
        """Release any underlying network resources.

        ``HttpReShareClient`` closes its ``httpx.AsyncClient``;
        ``MockReShareClient`` is a no-op. The FastAPI lifespan calls
        this on shutdown.
        """
        ...


class HttpReShareClient:
    """Real HTTP client for FOLIO mod-rs.

    Paths and action strings verified against mod-rs master (see module
    docstring for source files and probe notes). Body shape
    (probe-confirmed 2026-05-06): camelCase fields are bound directly
    onto the ``PatronRequest`` Grails domain object — pass the caller's
    ``request_payload`` through verbatim and merge the chosen supplier
    under ``supplyingInstitutionSymbol``. Note that including
    ``supplyingInstitutionSymbol`` causes mod-rs to create the record
    under the *Responder* state model; Requester-side creation via the
    direct API is still unconfirmed against a real borrower-tenant.

    Auth: HTTP Basic is dev-only (works when hitting the module's
    direct port with permissions disabled). When ``OKAPI_URL`` is set
    in config, the client switches to the FOLIO Okapi token flow via
    :class:`OkapiAuth` (see ADR-0013) — required for any real
    consortium tenant where requests go through the Okapi gateway.
    """

    # Action strings honoured by mod-rs's PatronRequestController.
    # Source: service/src/main/groovy/org/orf/rs/statemodel/Actions.groovy
    _ACTION_REQUESTER_CANCEL = "requesterCancel"
    _ACTION_SUPPLIER_MARK_SHIPPED = "supplierMarkShipped"
    _ACTION_PATRON_RETURNED_ITEM = "patronReturnedItem"
    # Force-close: valid at all states (AvailableActionData.groovy).
    # Used by recall_request per ADR-0016 (no first-class recall exists).
    _ACTION_MANUAL_CLOSE = "manualClose"

    def __init__(self, settings: Settings | None = None):
        s = settings or get_settings()
        if not s.reshare_base_url:
            raise ClientError("RESHARE_BASE_URL not configured")
        self._base_url = s.reshare_base_url.rstrip("/")
        self._tenant = s.reshare_tenant
        # Auth strategy (per ADR-0013):
        # - If ``OKAPI_URL`` is set → Okapi token flow (production).
        # - Else if ``RESHARE_USER`` is set → HTTP Basic (dev only,
        #   works when hitting mod-rs module-direct with permissions
        #   disabled).
        # - Else no auth (anonymous; only useful against a wide-open
        #   sandbox).
        self._auth: httpx.Auth | None
        if s.okapi_url:
            self._auth = OkapiAuth(
                login_url=f"{s.okapi_url.rstrip('/')}/authn/login",
                tenant=s.reshare_tenant,
                username=s.reshare_user,
                password=s.reshare_password.get_secret_value(),
            )
        elif s.reshare_user:
            self._auth = httpx.BasicAuth(
                s.reshare_user, s.reshare_password.get_secret_value()
            )
        else:
            self._auth = None
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
            # Audit 2026-05-09 #7: do NOT echo the raw response body
            # into the exception message. mod-rs error responses
            # commonly include patron identifiers, the inbound request
            # body echoed back, and occasional auth-header hints — all
            # of which would land verbatim in ``outbox.last_error`` and
            # any caller that surfaces ``str(exc)``. Truncate to a
            # bounded snippet AND log the full body at DEBUG so an
            # operator with log access can still diagnose, while a
            # leaky-error-response surface (logs harvested by an
            # attacker, debug endpoint, etc.) doesn't bleed PII.
            log.debug(
                "reshare.client_error_body",
                path=path,
                status_code=resp.status_code,
                body=resp.text,
            )
            snippet = (resp.text or "").strip().replace("\n", " ")[:200]
            raise ClientError(
                f"{path}: HTTP {resp.status_code} (body truncated: {snippet!r})"
            )
        data: dict[str, Any] = resp.json()
        return data

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

    async def renew_request(
        self, *, idempotency_key: str, reshare_id: str, extension_days: int
    ) -> ReShareSendResult:
        # Sandbox-blocked: no borrower-initiated renewal action has been
        # confirmed in mod-rs Actions.groovy. This raises ClientError so
        # the outbox worker surfaces a dead-letter row for staff review —
        # the saga stays at RECEIVED and the forward event is already
        # committed. See ADR-0017 for the wire-level resolution path.
        raise ClientError(
            f"renew_request sandbox-blocked: no mod-rs renewal action verified "
            f"(reshare_id={reshare_id}, extension_days={extension_days}). "
            "See ADR-0017 for the resolution path."
        )

    async def recall_request(
        self, *, idempotency_key: str, reshare_id: str, reason: str
    ) -> ReShareSendResult:
        # ADR-0016 (2026-05-06): mod-rs has no requester-initiated recall
        # action.  ``manualClose`` is used as a prototype force-close:
        # it closes the local mod-rs record immediately with **no ISO
        # 18626 message sent to the supplier**.  Staff must follow up
        # manually after the saga reaches DISPUTED.  The method name
        # ``recall_request`` reflects the saga's intent; the wire
        # mechanism is force-close until a two-tenant sandbox confirms
        # Option A (ISO 18626 Cancel via ``message`` performAction).
        # See ADR-0016 for the full trade-off analysis.
        data = await self._perform_action(
            reshare_id=reshare_id,
            idempotency_key=idempotency_key,
            action=self._ACTION_MANUAL_CLOSE,
            action_params={"reason": reason},
        )
        return _parse(data)

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
    its ``code``.

    Probe-confirmed (2026-05-06): ``isoMessageId`` and
    ``supplyingAgencyId`` are **absent** from basic create/GET
    responses — they are not top-level fields on ``PatronRequest``.
    ``reshare_id`` is populated from ``id`` (UUID); ``hrid`` may be
    absent/null. ``iso_message_id`` and ``supplier_symbol`` will be
    empty strings unless the caller passes ``supplier_default``.
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

    async def renew_request(
        self, *, idempotency_key: str, reshare_id: str, extension_days: int
    ) -> ReShareSendResult:
        return await self._transition(
            idempotency_key=idempotency_key,
            reshare_id=reshare_id,
            new_state="Loaned",
            event={"event": "renew", "extension_days": extension_days},
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
        return HttpReShareClient(s)
    log.info("reshare.client.using_mock")
    return MockReShareClient()
