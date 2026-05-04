"""FastAPI application factory + routes.

The console exposes a small surface:
- ``/health``                       liveness/diagnostic
- ``POST /requests``                patron-side submit
- ``GET /sagas``                    list pending and active sagas
- ``GET /sagas/{id}``               full event timeline
- ``POST /sagas/{id}/approve``      commit gate AND run the forward step
- ``POST /sagas/{id}/reject``       cancel pending gate
- ``POST /sagas/{id}/compensate``   run compensator for a committed forward

Auth is intentionally *not* implemented in the prototype — see ADR-0007.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agora import __version__
from agora.agents.discovery import DiscoveryAgent
from agora.agents.tracking import OverdueScanner
from agora.agents.transaction import TransactionAgent
from agora.api.schemas import (
    ApprovalBody,
    CompensateBody,
    DiscoverBody,
    DiscoverResponse,
    HealthResponse,
    RejectionBody,
    SagaDetail,
    SagaEventOut,
    SagaSummary,
    StepRunResponse,
    SubmitRequestResponse,
)
from agora.clients.crossref import CrossrefClient, get_crossref_client
from agora.clients.ncip import MockNcipClient, NcipClient
from agora.clients.reshare import ReShareClient
from agora.clients.reshare import get_client as get_reshare_client
from agora.clients.sru import SruClient, get_sru_client
from agora.config import get_settings
from agora.logging import configure_logging, get_logger
from agora.models.events import NewSagaEvent, SagaEvent
from agora.models.lifecycle import (
    TERMINAL_STATES,
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.models.request import IllRequest
from agora.saga.context import SagaContext
from agora.saga.coordinator import (
    Coordinator,
    CoordinatorError,
    GateRequiredError,
)
from agora.saga.db import Saga, get_sessionmaker
from agora.saga.flows import build_registry
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import (
    SagaLedger,
    SagaNotFoundError,
    TerminalStateError,
)
from agora.saga.outbox import (
    OutboxWorker,
    make_ncip_handler,
    make_reshare_handler,
    make_reshare_on_success,
)
from agora.saga.steps import StepRegistry

log = get_logger(__name__)


# Steps that can be approved + run via /approve. SUBMIT is not gated;
# it commits at /requests. Compensator-only steps (CANCEL, REROUTE,
# REVOKE, RECALL, DISPUTE) are not directly approvable either.
_APPROVABLE_STEPS: frozenset[StepName] = frozenset(
    {
        StepName.ROUTE,
        StepName.APPROVE,
        StepName.SHIP,
        StepName.RECEIVE,
        StepName.RETURN_ITEM,
    }
)


# --------------------------------------------------------------------- helpers
# Defined ahead of ``create_app`` so route handlers can reference them as
# default-arg ``Depends(...)`` values, which evaluate at function-definition
# time (i.e. when ``create_app`` runs).


async def _get_session() -> AsyncIterator[AsyncSession]:  # FastAPI dependency
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session


def _get_registry(request: Request) -> StepRegistry:
    """FastAPI dependency: return the per-app step registry."""
    return request.app.state.registry  # type: ignore[no-any-return]


def _to_summary(saga: Saga) -> SagaSummary:
    raw = saga.request_payload or {}
    item = raw.get("item") or {}
    requesting = raw.get("requesting_library") or {}
    return SagaSummary(
        saga_id=saga.id,
        request_id=saga.request_id,
        current_state=saga.current_state,
        iso18626_state=saga.iso18626_state,
        created_at=saga.created_at,
        updated_at=saga.updated_at,
        title=str(item.get("title") or ""),
        requesting_library=str(requesting.get("symbol") or ""),
    )


def _to_inbox_row(saga: Saga) -> dict[str, Any]:
    """Render a Saga ORM row as a dict for the inbox.html template.

    Kept distinct from ``_to_summary`` (which serialises to JSON for
    the ``GET /sagas`` API consumer) so the UI can evolve its column
    set without dragging the API schema with it.
    """
    raw = saga.request_payload or {}
    item = raw.get("item") or {}
    patron = raw.get("patron") or {}
    requesting = raw.get("requesting_library") or {}
    patron_id = str(patron.get("patron_id") or "")
    library = str(requesting.get("symbol") or patron.get("library_symbol") or "")
    patron_label = (
        f"{patron_id} @ {library}" if patron_id and library else (patron_id or library or "")
    )
    state = saga.current_state
    try:
        is_terminal = LifecycleState(state) in TERMINAL_STATES
    except ValueError:
        is_terminal = False
    return {
        "saga_id_short": str(saga.id)[:8],
        "patron_label": patron_label,
        "item_title": str(item.get("title") or ""),
        "current_state": state,
        "is_terminal": is_terminal,
    }


def _parse_step(name: str) -> StepName:
    try:
        return StepName(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unknown step {name!r}") from exc


def _derive_extras(
    events: list[SagaEvent],
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reconstruct step-input extras from prior committed events.

    Walks events in seq order so the most recent commit wins. Compensators
    that reverse a step also clear the value the forward set, keeping the
    derived extras consistent with the saga's logical position.

    Two event kinds contribute to extras:

    - ``FORWARD`` — the steady-state source for ``chosen_supplier``
      (set by ROUTE forward) and historical ``reshare_id`` (older
      sagas where APPROVE forward was inline; ADR-0011 vintage).
    - ``OBSERVATION`` for ``StepName.APPROVE`` — the new home for
      ``reshare_id`` after ADR-0012 migrated APPROVE forward to the
      outbox. The outbox worker projects the supplier ack here.

    The caller's ``override`` (request-body ``extras``) is merged last so
    a staff member can supply or correct an input the ledger doesn't have
    (typical for the first ROUTE call where no prior event names a supplier).
    """
    extras: dict[str, Any] = {}
    for ev in events:
        if ev.outcome != StepOutcome.COMMITTED:
            continue
        payload = ev.payload or {}

        if ev.kind == EventKind.FORWARD:
            if (
                ev.step in (StepName.ROUTE, StepName.APPROVE)
                and payload.get("supplier_symbol")
            ):
                extras["chosen_supplier"] = payload["supplier_symbol"]
            if payload.get("reshare_id"):
                extras["reshare_id"] = payload["reshare_id"]
        elif ev.kind == EventKind.OBSERVATION and ev.step == StepName.APPROVE:
            # ADR-0012: outbox-worker projection of the supplier ack
            # carries reshare_id (and supplier_symbol) onto the ledger.
            if payload.get("reshare_id"):
                extras["reshare_id"] = payload["reshare_id"]
            if payload.get("supplier_symbol"):
                extras["chosen_supplier"] = payload["supplier_symbol"]
        elif ev.kind == EventKind.COMPENSATOR:
            # Reverse what the paired forward set so the next attempt
            # at that step requires a fresh input.
            if ev.step == StepName.ROUTE:
                extras.pop("chosen_supplier", None)
            elif ev.step == StepName.APPROVE:
                extras.pop("reshare_id", None)

    if override:
        for k, v in override.items():
            if v is not None:
                extras[k] = v
    return extras


def _build_outbox_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    reshare: ReShareClient,
    ncip: NcipClient,
    max_attempts: int,
) -> OutboxWorker:
    """Construct a fully-wired :class:`OutboxWorker`.

    Single source of truth for the worker's handler + ``on_success``
    layout so the API lifespan and integration tests build exactly
    the same worker — drift here would silently change projection
    behaviour between production and tests, which would invalidate
    the e2e coverage of ADR-0012.

    Threads the ReShare ``send_request`` → APPROVING-to-APPROVED
    projection (ADR-0012) into ``on_success['reshare']``. Other
    targets register a handler only.
    """
    return OutboxWorker(
        sessionmaker,
        handlers={
            "reshare": make_reshare_handler(reshare),
            "ncip": make_ncip_handler(ncip),
        },
        on_success={
            # send_request projection: supplier ack lands as an
            # OBSERVATION advancing APPROVING -> APPROVED. ADR-0012.
            "reshare": make_reshare_on_success(),
        },
        max_attempts=max_attempts,
    )


def _make_context(
    *,
    saga_id: UUID,
    request: IllRequest,
    current_state: LifecycleState,
    actor: str,
    step: StepName,
    extras: dict[str, Any],
    idem_prefix: str | None = None,
) -> SagaContext:
    """Build a SagaContext for the coordinator."""
    return SagaContext(
        saga_id=saga_id,
        request=request,
        current_state=current_state,
        idempotency_key=new_idempotency_key(prefix=idem_prefix or step.value),
        actor=actor,
        extras=extras,
    )


def create_app() -> FastAPI:
    """Build the FastAPI app. One per process is sufficient.

    The app stashes the saga registry + the underlying ReShare client
    on ``app.state`` so request handlers can resolve them via dependency
    without rebuilding closures on every call (which would otherwise
    trip ``StepRegistry.register``'s same-name-different-callable check).

    The ReShare client is selected by :func:`agora.clients.reshare.get_client`
    based on settings: ``HttpReShareClient`` when ``RESHARE_BASE_URL``
    is set (with ``OkapiAuth`` if ``OKAPI_URL`` is also set per
    ADR-0013), otherwise ``MockReShareClient``. The lifespan calls
    ``aclose()`` on shutdown so the underlying ``httpx.AsyncClient``
    connection pool is released cleanly.

    Startup spawns two background tasks:
    - :class:`OutboxWorker` — polls the ``outbox`` table and dispatches
      pending rows. Disable via ``AGORA_OUTBOX_WORKER_ENABLED=0``.
    - :class:`OverdueScanner` — periodically scans shipped sagas for
      overdue items and writes idempotent OBSERVATION events. Disable
      via ``AGORA_TRACKING_SCANNER_ENABLED=0``.

    Tests using ``httpx.ASGITransport`` do not trigger the lifespan, so
    no background tasks spawn there; tests that explicitly need them
    can enter the lifespan context manually.
    """
    configure_logging()
    settings = get_settings()

    # Wire saga step registry. ``get_reshare_client`` honours
    # ``settings.reshare_enabled`` and returns ``HttpReShareClient``
    # in production / ``MockReShareClient`` for offline dev + tests.
    reshare = get_reshare_client()
    # NCIP client is mock-only today (CLAUDE.md known-gap). Constructed
    # here so the handler is wired into the outbox worker once and shares
    # process lifetime with the API.
    ncip = MockNcipClient()
    transaction = TransactionAgent(reshare)
    registry = build_registry(transaction)

    # DiscoveryAgent is constructed at app build time alongside reshare
    # so ASGI-transport tests (which skip the lifespan) can still hit
    # ``POST /sagas/{id}/discover``. Both clients honour their
    # ``AGORA_*_ENABLED`` toggles — mock by default for offline dev,
    # http when explicitly opted-in. ``consortium_members`` is parsed
    # from ``AGORA_CONSORTIUM_MEMBERS`` (comma-separated agency symbols)
    # via ``Settings.consortium_members``; empty default preserves the
    # pre-PR behaviour where every candidate's ``in_consortium`` flag
    # was false.
    crossref: CrossrefClient = get_crossref_client()
    sru: SruClient = get_sru_client()
    discovery = DiscoveryAgent(
        sru, crossref=crossref, consortium_members=settings.consortium_members
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Spawn outbox worker + tracking scanner on startup; cancel on shutdown.

        Two background tasks share this lifespan:
        - ``OutboxWorker`` drains ``outbox`` rows via target handlers.
        - ``OverdueScanner`` periodically observes overdue shipped sagas.

        Both honour their own ``*_ENABLED`` env flag so they can be
        disabled independently (e.g. when running migrations or in
        tests that don't enter the lifespan).
        """
        worker_task: asyncio.Task[None] | None = None
        scanner_task: asyncio.Task[None] | None = None

        if settings.outbox_worker_enabled:
            worker = _build_outbox_worker(
                get_sessionmaker(),
                reshare=reshare,
                ncip=ncip,
                max_attempts=settings.outbox_retry_max_attempts,
            )
            worker_task = asyncio.create_task(
                worker.run_forever(
                    poll_interval=settings.outbox_poll_interval_secs
                ),
                name="agora.outbox.worker",
            )
            app.state.outbox_worker = worker
            app.state.outbox_worker_task = worker_task
            log.info(
                "api.outbox_worker.started",
                poll_interval=settings.outbox_poll_interval_secs,
            )
        else:
            app.state.outbox_worker = None
            app.state.outbox_worker_task = None
            log.info("api.outbox_worker.disabled")

        if settings.tracking_scanner_enabled:
            scanner = OverdueScanner(
                get_sessionmaker(),
                recall_after_days=settings.tracking_recall_after_days,
            )
            scanner_task = asyncio.create_task(
                scanner.run_forever(
                    poll_interval=settings.tracking_scan_interval_secs
                ),
                name="agora.tracking.scanner",
            )
            app.state.tracking_scanner = scanner
            app.state.tracking_scanner_task = scanner_task
            log.info(
                "api.tracking_scanner.started",
                poll_interval=settings.tracking_scan_interval_secs,
            )
        else:
            app.state.tracking_scanner = None
            app.state.tracking_scanner_task = None
            log.info("api.tracking_scanner.disabled")

        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
                log.info("api.outbox_worker.stopped")
            if scanner_task is not None:
                scanner_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await scanner_task
                log.info("api.tracking_scanner.stopped")
            # Release the ReShare client's connection pool. ``aclose``
            # is a no-op on the mock; on ``HttpReShareClient`` it
            # closes the underlying ``httpx.AsyncClient``.
            await reshare.aclose()
            log.info("api.reshare_client.closed")
            # Same shape for the discovery clients: mocks no-op, http
            # impls release the underlying ``httpx.AsyncClient`` pool.
            await crossref.aclose()
            await sru.aclose()
            log.info("api.discovery_clients.closed")

    app = FastAPI(
        title="Agora ILL",
        description="Agentic Inter-Library Loan staff console",
        version=__version__,
        lifespan=lifespan,
    )

    app.state.registry = registry
    app.state.reshare = reshare
    app.state.ncip = ncip
    app.state.discovery = discovery
    # Ensure attributes always exist so dependents can read them even
    # when the lifespan never runs (e.g. ASGI transports that skip it).
    app.state.outbox_worker = None
    app.state.outbox_worker_task = None
    app.state.tracking_scanner = None
    app.state.tracking_scanner_task = None

    # Staff console UI (HTMX + Jinja2 — see ADR-0015). Templates are
    # colocated with the API package so they ship inside the wheel
    # alongside the routes that render them.
    _ui_root = Path(__file__).resolve().parent
    app.mount(
        "/static",
        StaticFiles(directory=str(_ui_root / "static")),
        name="static",
    )
    templates = Jinja2Templates(directory=str(_ui_root / "templates"))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def staff_console_inbox(
        request: Request,
        session: AsyncSession = Depends(_get_session),
    ) -> HTMLResponse:
        """Staff console inbox — HTML view over recent sagas.

        First UI slice (ADR-0015): read-only table. Approve / Reject /
        Compensate land in a follow-up PR; today the JSON endpoints
        remain the only state-changing surface.
        """
        async with session.begin():
            stmt = select(Saga).order_by(Saga.updated_at.desc()).limit(200)
            rows = (await session.execute(stmt)).scalars().all()
            ctx_sagas = [_to_inbox_row(saga) for saga in rows]
        return templates.TemplateResponse(
            request,
            "inbox.html",
            {"sagas": ctx_sagas},
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", env=settings.env, version=__version__)

    @app.post(
        "/requests",
        response_model=SubmitRequestResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def submit_request(
        request: IllRequest,
        session: AsyncSession = Depends(_get_session),
    ) -> SubmitRequestResponse:
        async with session.begin():
            ledger = SagaLedger(session)
            saga_id = uuid4()
            await ledger.create_saga(
                saga_id=saga_id,
                request_id=request.request_id,
                request_payload=request.model_dump(mode="json"),
                initial_state=LifecycleState.SUBMITTED,
            )
            await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.FORWARD,
                    step=StepName.SUBMIT,
                    state_before=LifecycleState.SUBMITTED,
                    state_after=LifecycleState.SUBMITTED,
                    actor="patron",
                    idempotency_key=new_idempotency_key(prefix="submit"),
                    payload={"submitted_at": datetime.now(UTC).isoformat()},
                    outcome=StepOutcome.COMMITTED,
                    rationale="Patron submitted ILL request.",
                )
            )
        return SubmitRequestResponse(saga_id=saga_id, request=request)

    @app.get("/sagas", response_model=list[SagaSummary])
    async def list_sagas(
        session: AsyncSession = Depends(_get_session),
    ) -> list[SagaSummary]:
        async with session.begin():
            stmt = select(Saga).order_by(Saga.updated_at.desc()).limit(200)
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_summary(r) for r in rows]

    @app.get("/sagas/{saga_id}", response_model=SagaDetail)
    async def get_saga(
        saga_id: UUID,
        session: AsyncSession = Depends(_get_session),
    ) -> SagaDetail:
        async with session.begin():
            saga = await session.get(Saga, saga_id)
            if saga is None:
                raise HTTPException(status_code=404, detail="saga not found")
            ledger = SagaLedger(session)
            events = await ledger.events_for(saga_id)
            return SagaDetail(
                saga=_to_summary(saga),
                events=[
                    SagaEventOut.model_validate(e.model_dump()) for e in events
                ],
            )

    @app.post(
        "/sagas/{saga_id}/approve",
        response_model=StepRunResponse,
        status_code=status.HTTP_200_OK,
    )
    async def approve(
        saga_id: UUID,
        body: ApprovalBody,
        session: AsyncSession = Depends(_get_session),
        registry: StepRegistry = Depends(_get_registry),
    ) -> StepRunResponse:
        """Commit the gate for ``body.step`` and run the forward step.

        Both writes (gate-commit + forward event) happen in a single DB
        transaction; if the forward fails, the gate-commit also rolls
        back so staff can retry cleanly.
        """
        step = _parse_step(body.step)
        if step not in _APPROVABLE_STEPS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"step {step.value!r} is not approvable via this endpoint; "
                    f"valid steps: {sorted(s.value for s in _APPROVABLE_STEPS)}"
                ),
            )

        try:
            async with session.begin():
                coord = Coordinator(session=session, registry=registry)
                ledger = SagaLedger(session)

                # 1. Commit the gate (records staff approval).
                await coord.commit_gate(
                    saga_id=saga_id,
                    step=step,
                    actor=body.actor,
                    rationale=body.rationale,
                )

                # 2. Reload events so derivation sees the just-committed
                #    gate plus all prior committed forwards.
                saga = await ledger.get_saga(saga_id)
                events = await ledger.events_for(saga_id)
                extras = _derive_extras(events, body.extras)

                # 3. Build context and run the forward step.
                request = IllRequest.model_validate(saga.request_payload)
                ctx_actor = body.actor
                ev = await coord.run_forward(
                    ctx=_make_context(
                        saga_id=saga_id,
                        request=request,
                        current_state=LifecycleState(saga.current_state),
                        actor=ctx_actor,
                        step=step,
                        extras=extras,
                    ),
                    step=step,
                )
            return StepRunResponse.model_validate(ev.model_dump())
        except SagaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GateRequiredError as exc:  # defensive: we just committed it
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TerminalStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            # Most common: forward step missing a required ``extras`` key.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CoordinatorError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post(
        "/sagas/{saga_id}/discover",
        response_model=DiscoverResponse,
        status_code=status.HTTP_200_OK,
    )
    async def discover(
        saga_id: UUID,
        http_request: Request,
        body: DiscoverBody | None = None,
        session: AsyncSession = Depends(_get_session),
    ) -> DiscoverResponse:
        """Run DiscoveryAgent against the saga's stored request.

        Advisory only: writes a single ``DISCOVERY`` OBSERVATION event
        with candidates + diagnostics + rationale. The saga state is
        unchanged — staff still has to commit a ROUTE gate before the
        chosen supplier is locked in. ``StepName.ROUTE`` anchors the
        observation because discovery candidates feed routing input.

        Re-runnable by design: each call generates a fresh ULID
        idempotency key so a citation update or SRU index refresh
        produces a new event. Staff console renders the latest
        DISCOVERY observation as the live candidate list.
        """
        actor = body.actor if body is not None else "agent:discovery"
        agent: DiscoveryAgent = http_request.app.state.discovery

        try:
            async with session.begin():
                ledger = SagaLedger(session)
                saga = await ledger.get_saga(saga_id)
                current = LifecycleState(saga.current_state)
                if current in TERMINAL_STATES:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"saga is in terminal state {current.value!r}; "
                            "discovery is advisory and only runs on active sagas"
                        ),
                    )

                ill_request = IllRequest.model_validate(saga.request_payload)
                rec = await agent.run(ill_request)

                payload: dict[str, Any] = {
                    "kind": "discovery",
                    "candidates": [c.model_dump(mode="json") for c in rec.candidates],
                    "diagnostics": list(rec.diagnostics),
                    "observed_at": datetime.now(UTC).isoformat(),
                }
                ev = await ledger.append(
                    NewSagaEvent(
                        saga_id=saga_id,
                        kind=EventKind.OBSERVATION,
                        step=StepName.ROUTE,
                        state_before=current,
                        state_after=current,
                        actor=actor,
                        idempotency_key=new_idempotency_key(prefix="discovery"),
                        payload=payload,
                        outcome=StepOutcome.COMMITTED,
                        rationale=rec.rationale,
                    )
                )
            return DiscoverResponse(
                saga_id=saga_id,
                event=SagaEventOut.model_validate(ev.model_dump()),
                candidates=rec.candidates,
                diagnostics=list(rec.diagnostics),
                rationale=rec.rationale,
            )
        except SagaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/sagas/{saga_id}/reject", status_code=204)
    async def reject(
        saga_id: UUID,
        body: RejectionBody,
        session: AsyncSession = Depends(_get_session),
    ) -> None:
        step = _parse_step(body.step)
        async with session.begin():
            ledger = SagaLedger(session)
            saga = await ledger.get_saga(saga_id)
            await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.GATE,
                    step=step,
                    state_before=LifecycleState(saga.current_state),
                    state_after=LifecycleState(saga.current_state),
                    actor=body.actor,
                    idempotency_key=new_idempotency_key(prefix="gate-reject"),
                    payload={"reason": body.rationale},
                    outcome=StepOutcome.FAILED,
                    rationale=body.rationale,
                )
            )

    @app.post(
        "/sagas/{saga_id}/compensate",
        response_model=StepRunResponse,
        status_code=status.HTTP_200_OK,
    )
    async def compensate(
        saga_id: UUID,
        body: CompensateBody,
        session: AsyncSession = Depends(_get_session),
        registry: StepRegistry = Depends(_get_registry),
    ) -> StepRunResponse:
        """Run the compensator for ``body.step`` against its committed forward.

        Returns 409 if no committed forward exists for the step (nothing
        to undo) or if the saga is already terminal.
        """
        step = _parse_step(body.step)

        try:
            async with session.begin():
                coord = Coordinator(session=session, registry=registry)
                ledger = SagaLedger(session)
                saga = await ledger.get_saga(saga_id)
                events = await ledger.events_for(saga_id)
                extras = _derive_extras(events, body.extras)

                request = IllRequest.model_validate(saga.request_payload)
                ev = await coord.run_compensator(
                    ctx=_make_context(
                        saga_id=saga_id,
                        request=request,
                        current_state=LifecycleState(saga.current_state),
                        actor=body.actor,
                        step=step,
                        extras=extras,
                        idem_prefix=f"comp-{step.value}",
                    ),
                    step=step,
                )
            return StepRunResponse.model_validate(ev.model_dump())
        except SagaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except TerminalStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CoordinatorError as exc:
            # Includes "no committed forward for step ..." -> 409.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


# Module-level instance for ``uvicorn agora.api.app:app``.
app = create_app()
