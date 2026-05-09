"""FastAPI application factory + routes.

The console exposes a small surface:
- ``/health``                       liveness/diagnostic
- ``POST /requests``                patron-side submit
- ``GET /sagas``                    list pending and active sagas
- ``GET /sagas/{id}``               full event timeline
- ``POST /sagas/{id}/approve``      commit gate AND run the forward step
- ``POST /sagas/{id}/reject``       cancel pending gate
- ``POST /sagas/{id}/compensate``   run compensator for a committed forward
- ``POST /sagas/{id}/override``     resolve DISPUTED saga to CANCELLED/UNFILLED

Auth is intentionally *not* implemented in the prototype — see ADR-0007.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from datetime import date as _date
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
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
    OverrideBody,
    RejectionBody,
    RenewBody,
    SagaDetail,
    SagaEventOut,
    SagaSummary,
    StepRunResponse,
    SubmitRequestResponse,
)
from agora.clients.crossref import CrossrefClient, get_crossref_client
from agora.clients.ncip import NcipClient
from agora.clients.ncip import get_client as get_ncip_client
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
        "saga_id": str(saga.id),
        "saga_id_short": str(saga.id)[:8],
        "patron_label": patron_label,
        "item_title": str(item.get("title") or ""),
        "current_state": state,
        "is_terminal": is_terminal,
    }


def _portal_due_date(events: list[SagaEvent]) -> str:
    """Return the effective loan due date from the event stream.

    Walks committed events in ``seq`` order (guaranteed by
    ``SagaLedger.events_for``):

    - ``forward.ship.due_at`` seeds the base loan period.
    - Each ``forward.renew`` pushes its ``new_due_at`` onto a stack and
      becomes the current effective due date.
    - Each committed ``compensator.renew`` pops the most recent renewal,
      restoring the previous due date (the prior renewal's ``new_due_at``
      or, when the stack is empty, the SHIP ``due_at``).

    Without compensator handling a forward+compensator pair would leave
    the portal showing the cancelled renewal's due date.
    """
    ship_due: str = ""
    renew_stack: list[str] = []
    for ev in events:
        if ev.outcome != StepOutcome.COMMITTED:
            continue
        payload = ev.payload or {}
        if ev.kind == EventKind.FORWARD and ev.step.value == "ship" and payload.get("due_at"):
            ship_due = str(payload["due_at"])[:10]
        elif ev.kind == EventKind.FORWARD and ev.step.value == "renew" and payload.get("new_due_at"):
            renew_stack.append(str(payload["new_due_at"])[:10])
        elif ev.kind == EventKind.COMPENSATOR and ev.step.value == "renew" and renew_stack:
            renew_stack.pop()
    return renew_stack[-1] if renew_stack else ship_due


_PATRON_EVENT_LABELS: dict[tuple[str, str], str] = {
    ("forward", "submit"): "Request submitted",
    ("forward", "route"): "Supplier identified",
    ("observation", "approve"): "Request confirmed by supplier",
    ("forward", "ship"): "Item shipped",
    ("forward", "receive"): "Item received — loan started",
    ("forward", "return"): "Item returned",
    ("forward", "renew"): "Loan renewed",
    ("compensator", "ship"): "Item recalled",
    ("compensator", "receive"): "Receipt under review",
    ("compensator", "return"): "Return under review",
    ("compensator", "renew"): "Renewal cancelled",
    ("compensator", "approve"): "Request cancelled",
    ("compensator", "submit"): "Request cancelled",
}


def _patron_event_label(ev: SagaEvent) -> str | None:
    """Return a patron-facing label for ``ev``, or None to skip it."""
    return _PATRON_EVENT_LABELS.get((ev.kind.value, ev.step.value))


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


def _log_background_task_exit(task: asyncio.Task[None]) -> None:
    """Log unexpected background-task exits so silent death is visible.

    A bug in :meth:`OutboxWorker.run_forever` or
    :meth:`OverdueScanner.run_forever` that escapes the inner
    try/except leaves the task in ``done`` with an unretrieved
    exception. Without this callback, no other code awaits the task
    during normal operation, so its failure is invisible — outbox
    rows pile up forever, the scanner stops emitting overdue
    observations, and there is no signal to staff.

    A normal cancel-on-shutdown raises ``CancelledError`` which we
    classify as expected and log at INFO. Any other exception is logged
    at ERROR with the task name and exc_info; restart-on-failure is
    NOT attempted here because the right operator response depends on
    the failure mode (DB outage, schema drift, code bug).

    Audit 2026-05-09 #29.
    """
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        log.info("api.background_task.cancelled", task_name=task.get_name())
        return
    if exc is None:
        log.info("api.background_task.exited", task_name=task.get_name())
        return
    log.error(
        "api.background_task.failed",
        task_name=task.get_name(),
        error=str(exc),
        exc_info=(type(exc), exc, exc.__traceback__),
    )


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
    idempotency_key: str | None = None,
) -> SagaContext:
    """Build a SagaContext for the coordinator.

    Pass ``idempotency_key`` to use a fully-deterministic key (e.g. for
    compensators where collision-on-replay is the desired behaviour;
    audit 2026-05-09 #5). Otherwise a fresh ULID is appended to
    ``idem_prefix`` (or ``step.value``) for forward steps where each
    invocation must produce a distinct event.
    """
    if idempotency_key is None:
        idempotency_key = new_idempotency_key(prefix=idem_prefix or step.value)
    return SagaContext(
        saga_id=saga_id,
        request=request,
        current_state=current_state,
        idempotency_key=idempotency_key,
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
    # NCIP client: real HttpNcipClient when NCIP_BASE_URL is set,
    # MockNcipClient otherwise (offline dev + tests). Source-review-only
    # against mod-ncip master (2026-05-06) — live tenant probe still
    # needed before production use. See CLAUDE.md known-gaps.
    ncip = get_ncip_client()
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
            worker_task.add_done_callback(_log_background_task_exit)
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
            scanner_task.add_done_callback(_log_background_task_exit)
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
            # NCIP: mock no-ops; HttpNcipClient closes its httpx pool.
            await ncip.aclose()
            log.info("api.ncip_client.closed")
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

    # HTTP Basic auth guard for the HTML console.
    # When AGORA_CONSOLE_PASSWORD is empty (default) the check is skipped —
    # no credentials required in local dev. Set both vars to enable.
    _console_security = HTTPBasic(auto_error=False)

    def _require_console_auth(
        credentials: HTTPBasicCredentials | None = Depends(_console_security),
        settings: Any = Depends(get_settings),
    ) -> None:
        password = settings.console_password
        if not password:
            return  # auth disabled
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic realm='Agora staff console'"},
            )
        user_ok = secrets.compare_digest(
            credentials.username.encode(), settings.console_username.encode()
        )
        pass_ok = secrets.compare_digest(credentials.password.encode(), password.encode())
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic realm='Agora staff console'"},
            )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def staff_console_inbox(
        request: Request,
        session: AsyncSession = Depends(_get_session),
        _auth: None = Depends(_require_console_auth),
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

    @app.get("/browser", response_class=HTMLResponse, include_in_schema=False)
    async def saga_browser(
        request: Request,
        session: AsyncSession = Depends(_get_session),
        _auth: None = Depends(_require_console_auth),
        state: str | None = Query(default=None),
        library: str | None = Query(default=None),
        date_from: str | None = Query(default=None),
        date_to: str | None = Query(default=None),
    ) -> HTMLResponse:
        """Saga browser — filter by state, requesting library, and/or date range.

        All filters are optional and combinable. State must be a valid
        ``LifecycleState`` value; unrecognised values are silently ignored.
        Library is a case-insensitive substring match against
        ``requesting_library.symbol`` in the stored request payload.
        Date range applies to ``created_at`` (UTC).
        """
        async with session.begin():
            stmt = select(Saga).order_by(Saga.created_at.desc()).limit(500)

            # SQL-native filters (indexed columns).
            if state:
                try:
                    LifecycleState(state)  # validate
                    stmt = stmt.where(Saga.current_state == state)
                except ValueError:
                    state = None  # drop invalid value; show all states

            if date_from:
                try:
                    df = _date.fromisoformat(date_from)
                    stmt = stmt.where(
                        Saga.created_at >= datetime(df.year, df.month, df.day, tzinfo=UTC)
                    )
                except ValueError:
                    date_from = None

            if date_to:
                try:
                    dt = _date.fromisoformat(date_to)
                    # inclusive: up to end-of-day on date_to
                    stmt = stmt.where(
                        Saga.created_at
                        < datetime(dt.year, dt.month, dt.day, tzinfo=UTC)
                        + timedelta(days=1)
                    )
                except ValueError:
                    date_to = None

            rows = (await session.execute(stmt)).scalars().all()

        # Python-side library filter (request_payload is JSON, not indexable).
        ctx_sagas = [_to_inbox_row(saga) for saga in rows]
        if library:
            needle = library.strip().lower()
            ctx_sagas = [
                r for r in ctx_sagas if needle in r["patron_label"].lower()
            ]

        all_states = [s.value for s in LifecycleState]
        return templates.TemplateResponse(
            request,
            "browser.html",
            {
                "sagas": ctx_sagas,
                "all_states": all_states,
                "filter_state": state or "",
                "filter_library": library or "",
                "filter_date_from": date_from or "",
                "filter_date_to": date_to or "",
                "total": len(ctx_sagas),
            },
        )

    # ------------------------------------------------------------------
    # Staff console UI — detail view + form action endpoints (slice 2)
    # ------------------------------------------------------------------

    # Maps a saga's current_state to the forward step staff can approve next.
    # APPROVING is intentionally absent: the outbox worker is in flight and
    # there is nothing for staff to do until the ack lands (or the row dies).
    _STATE_TO_APPROVE_STEP: dict[str, str] = {
        "submitted": StepName.ROUTE.value,
        "routed": StepName.APPROVE.value,
        "approved": StepName.SHIP.value,
        "shipped": StepName.RECEIVE.value,
        "received": StepName.RETURN_ITEM.value,
    }

    # Maps current_state to the most-recently-committed forward that can be
    # compensated. APPROVING is absent: approve_compensator 400s (no reshare_id).
    _STATE_TO_COMPENSATE_STEP: dict[str, str] = {
        "routed": StepName.ROUTE.value,
        "approved": StepName.APPROVE.value,
        "shipped": StepName.SHIP.value,
        "received": StepName.RECEIVE.value,
    }

    @app.get("/sagas/{saga_id}/view", response_class=HTMLResponse, include_in_schema=False)
    async def saga_detail_view(
        saga_id: UUID,
        request: Request,
        session: AsyncSession = Depends(_get_session),
        _auth: None = Depends(_require_console_auth),
    ) -> HTMLResponse:
        """Staff console detail view — full ledger timeline + action forms."""
        async with session.begin():
            saga = await session.get(Saga, saga_id)
            if saga is None:
                raise HTTPException(status_code=404, detail="saga not found")
            ledger = SagaLedger(session)
            events = await ledger.events_for(saga_id)

        row = _to_inbox_row(saga)
        state = row["current_state"]
        approve_step = _STATE_TO_APPROVE_STEP.get(state)
        compensate_step = _STATE_TO_COMPENSATE_STEP.get(state)

        event_rows = [
            {
                "seq": ev.seq,
                "kind": ev.kind.value,
                "step": ev.step.value,
                "state_before": ev.state_before.value,
                "state_after": ev.state_after.value,
                "actor": ev.actor or "",
                "outcome": ev.outcome.value,
                "rationale": ev.rationale or "",
                "ts": ev.ts.strftime("%Y-%m-%d %H:%M UTC") if ev.ts else "",
            }
            for ev in events
        ]

        # Last discovery OBSERVATION for the panel pre-render (item 4).
        cached_discovery: dict[str, Any] | None = None
        for ev in reversed(events):
            if (
                ev.kind == EventKind.OBSERVATION
                and ev.step == StepName.ROUTE
                and isinstance(ev.payload, dict)
                and ev.payload.get("kind") == "discovery"
            ):
                raw = ev.payload.get("candidates", [])
                cached_discovery = {
                    "candidates": [
                        {
                            "symbol": c["symbol"],
                            "name": c.get("name") or "",
                            "status": c.get("status", "unknown"),
                            "distance_km": round(c["distance_km"], 1) if c.get("distance_km") is not None else "",
                            "in_consortium": c.get("is_consortium_member", False),
                        }
                        for c in raw
                    ],
                    "diagnostics": ev.payload.get("diagnostics", []),
                    "rationale": ev.rationale or "",
                    "observed_at": ev.payload.get("observed_at", "")[:10],
                }
                break

        can_route = state == LifecycleState.SUBMITTED.value
        can_renew = state == LifecycleState.RECEIVED.value
        show_override = state == LifecycleState.DISPUTED.value
        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "saga": row,
                "events": event_rows,
                "approve_step": approve_step,
                "compensate_step": compensate_step,
                "cached_discovery": cached_discovery,
                "can_route": can_route,
                "can_renew": can_renew,
                "show_override": show_override,
                "saga_id": str(saga_id),
            },
        )

    @app.post("/ui/sagas/{saga_id}/approve", include_in_schema=False)
    async def ui_saga_approve(
        saga_id: UUID,
        session: AsyncSession = Depends(_get_session),
        registry: StepRegistry = Depends(_get_registry),
        _auth: None = Depends(_require_console_auth),
        step: str = Form(...),
        rationale: str = Form("Staff approved."),
        chosen_supplier: str = Form(""),
    ) -> RedirectResponse:
        """HTML form endpoint: commit gate + run forward, then redirect to detail."""
        step_name = _parse_step(step)
        if step_name not in _APPROVABLE_STEPS:
            raise HTTPException(status_code=400, detail=f"step {step!r} is not approvable")

        extras: dict[str, Any] = {}
        if chosen_supplier:
            extras["chosen_supplier"] = chosen_supplier

        try:
            async with session.begin():
                coord = Coordinator(session=session, registry=registry)
                ledger = SagaLedger(session)
                await coord.commit_gate(
                    saga_id=saga_id,
                    step=step_name,
                    actor="staff",
                    rationale=rationale,
                )
                saga = await ledger.get_saga(saga_id)
                events = await ledger.events_for(saga_id)
                full_extras = _derive_extras(events, extras or None)
                ill_request = IllRequest.model_validate(saga.request_payload)
                await coord.run_forward(
                    ctx=_make_context(
                        saga_id=saga_id,
                        request=ill_request,
                        current_state=LifecycleState(saga.current_state),
                        actor="staff",
                        step=step_name,
                        extras=full_extras,
                    ),
                    step=step_name,
                )
        except (SagaNotFoundError, GateRequiredError, TerminalStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (CoordinatorError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return RedirectResponse(url=f"/sagas/{saga_id}/view", status_code=303)

    @app.post("/ui/sagas/{saga_id}/discover", response_class=HTMLResponse, include_in_schema=False)
    async def ui_saga_discover(
        saga_id: UUID,
        http_request: Request,
        session: AsyncSession = Depends(_get_session),
        _auth: None = Depends(_require_console_auth),
    ) -> HTMLResponse:
        """HTMX partial: run DiscoveryAgent, return candidate list as HTML fragment.

        The detail view swaps this fragment into ``#discovery-panel`` via
        ``hx-post`` + ``hx-swap="outerHTML"``.  The partial includes inline
        Select-and-approve mini-forms for each candidate so staff can pick a
        supplier without copying a symbol manually.
        """
        agent: DiscoveryAgent = http_request.app.state.discovery

        try:
            async with session.begin():
                ledger = SagaLedger(session)
                saga = await ledger.get_saga(saga_id)
                current = LifecycleState(saga.current_state)
                if current in TERMINAL_STATES:
                    raise HTTPException(
                        status_code=409,
                        detail=f"saga is terminal ({current.value}); discovery only runs on active sagas",
                    )
                ill_request = IllRequest.model_validate(saga.request_payload)
                rec = await agent.run(ill_request)

                payload: dict[str, Any] = {
                    "kind": "discovery",
                    "candidates": [c.model_dump(mode="json") for c in rec.candidates],
                    "diagnostics": list(rec.diagnostics),
                    "observed_at": datetime.now(UTC).isoformat(),
                }
                await ledger.append(
                    NewSagaEvent(
                        saga_id=saga_id,
                        kind=EventKind.OBSERVATION,
                        step=StepName.ROUTE,
                        state_before=current,
                        state_after=current,
                        actor="agent:discovery",
                        idempotency_key=new_idempotency_key(prefix="discovery"),
                        payload=payload,
                        outcome=StepOutcome.COMMITTED,
                        rationale=rec.rationale,
                    )
                )
        except SagaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        candidate_rows = [
            {
                "symbol": c.symbol,
                "name": c.name or "",
                "status": c.status,
                "distance_km": round(c.distance_km, 1) if c.distance_km is not None else "",
                "in_consortium": c.is_consortium_member,
            }
            for c in rec.candidates
        ]
        can_route = current == LifecycleState.SUBMITTED
        return templates.TemplateResponse(
            http_request,
            "_discover_panel.html",
            {
                "saga_id": str(saga_id),
                "candidates": candidate_rows,
                "diagnostics": list(rec.diagnostics),
                "rationale": rec.rationale,
                "can_route": can_route,
            },
        )

    @app.post("/ui/sagas/{saga_id}/reject", include_in_schema=False)
    async def ui_saga_reject(
        saga_id: UUID,
        session: AsyncSession = Depends(_get_session),
        _auth: None = Depends(_require_console_auth),
        step: str = Form(...),
        rationale: str = Form("Staff rejected."),
    ) -> RedirectResponse:
        """HTML form endpoint: append FAILED gate, then redirect to detail."""
        step_name = _parse_step(step)
        try:
            async with session.begin():
                ledger = SagaLedger(session)
                saga = await ledger.get_saga(saga_id)
                await ledger.append(
                    NewSagaEvent(
                        saga_id=saga_id,
                        kind=EventKind.GATE,
                        step=step_name,
                        state_before=LifecycleState(saga.current_state),
                        state_after=LifecycleState(saga.current_state),
                        actor="staff",
                        idempotency_key=new_idempotency_key(prefix="gate-reject"),
                        payload={"reason": rationale},
                        outcome=StepOutcome.FAILED,
                        rationale=rationale,
                    )
                )
        except SagaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return RedirectResponse(url=f"/sagas/{saga_id}/view", status_code=303)

    @app.post("/ui/sagas/{saga_id}/compensate", include_in_schema=False)
    async def ui_saga_compensate(
        saga_id: UUID,
        session: AsyncSession = Depends(_get_session),
        registry: StepRegistry = Depends(_get_registry),
        _auth: None = Depends(_require_console_auth),
        step: str = Form(...),
        rationale: str = Form("Staff compensated."),
    ) -> RedirectResponse:
        """HTML form endpoint: run compensator, then redirect to detail."""
        step_name = _parse_step(step)
        try:
            async with session.begin():
                coord = Coordinator(session=session, registry=registry)
                ledger = SagaLedger(session)
                saga = await ledger.get_saga(saga_id)
                events = await ledger.events_for(saga_id)
                extras = _derive_extras(events, None)
                ill_request = IllRequest.model_validate(saga.request_payload)
                await coord.run_compensator(
                    ctx=_make_context(
                        saga_id=saga_id,
                        request=ill_request,
                        current_state=LifecycleState(saga.current_state),
                        actor="staff",
                        step=step_name,
                        extras=extras,
                        # Deterministic key — second /compensate call
                        # collides on saga_event UNIQUE(idempotency_key)
                        # and ledger.append returns the prior event
                        # without re-firing the compensator. Audit
                        # 2026-05-09 #5 (defense in depth alongside the
                        # terminal-state guard at ledger.py:91-99).
                        idempotency_key=f"comp-{step_name.value}-{saga_id}",
                    ),
                    step=step_name,
                )
        except SagaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (TerminalStateError, CoordinatorError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return RedirectResponse(url=f"/sagas/{saga_id}/view", status_code=303)

    @app.post("/ui/sagas/{saga_id}/override", include_in_schema=False)
    async def ui_saga_override(
        saga_id: UUID,
        session: AsyncSession = Depends(_get_session),
        _auth: None = Depends(_require_console_auth),
        target_state: str = Form(...),
        rationale: str = Form("Staff override."),
        actor: str = Form("staff"),
    ) -> RedirectResponse:
        """HTML form endpoint: resolve DISPUTED saga, then redirect to detail."""
        try:
            target = LifecycleState(target_state)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"invalid target_state {target_state!r}; "
                    "allowed: cancelled, unfilled"
                ),
            ) from exc

        if target not in {LifecycleState.CANCELLED, LifecycleState.UNFILLED}:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"target_state {target.value!r} is not allowed; "
                    "allowed: cancelled, unfilled"
                ),
            )

        async with session.begin():
            ledger = SagaLedger(session)
            try:
                saga = await ledger.get_saga(saga_id)
            except SagaNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            current = LifecycleState(saga.current_state)
            if current != LifecycleState.DISPUTED:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"saga {saga_id} is in state {current.value!r}; "
                        "override only applies to sagas in 'disputed' state"
                    ),
                )

            await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.OBSERVATION,
                    step=StepName.RESOLVE,
                    state_before=current,
                    state_after=target,
                    actor=actor,
                    idempotency_key=new_idempotency_key("override"),
                    outcome=StepOutcome.COMMITTED,
                    rationale=rationale,
                    payload={"target_state": target.value},
                )
            )

        return RedirectResponse(url=f"/sagas/{saga_id}/view", status_code=303)

    @app.post("/ui/sagas/{saga_id}/renew", include_in_schema=False)
    async def ui_saga_renew(
        saga_id: UUID,
        session: AsyncSession = Depends(_get_session),
        registry: StepRegistry = Depends(_get_registry),
        _auth: None = Depends(_require_console_auth),
        extension_days: int = Form(28),
        rationale: str = Form("Staff approved renewal."),
    ) -> RedirectResponse:
        """HTML form endpoint: commit RENEW gate + run forward, redirect to detail."""
        try:
            async with session.begin():
                coord = Coordinator(session=session, registry=registry)
                ledger = SagaLedger(session)
                saga = await ledger.get_saga(saga_id)
                current = LifecycleState(saga.current_state)
                if current != LifecycleState.RECEIVED:
                    raise HTTPException(
                        status_code=409,
                        detail=f"renew requires 'received' state; got {current.value!r}",
                    )
                await coord.commit_gate(
                    saga_id=saga_id,
                    step=StepName.RENEW,
                    actor="staff",
                    rationale=rationale,
                )
                events = await ledger.events_for(saga_id)
                extras = _derive_extras(events, {"extension_days": extension_days})
                ill_request = IllRequest.model_validate(saga.request_payload)
                await coord.run_forward(
                    ctx=_make_context(
                        saga_id=saga_id,
                        request=ill_request,
                        current_state=current,
                        actor="staff",
                        step=StepName.RENEW,
                        extras=extras,
                    ),
                    step=StepName.RENEW,
                )
        except SagaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (GateRequiredError, TerminalStateError, CoordinatorError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return RedirectResponse(url=f"/sagas/{saga_id}/view", status_code=303)

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
        "/sagas/{saga_id}/renew",
        response_model=StepRunResponse,
        status_code=status.HTTP_200_OK,
    )
    async def renew(
        saga_id: UUID,
        body: RenewBody,
        session: AsyncSession = Depends(_get_session),
        registry: StepRegistry = Depends(_get_registry),
    ) -> StepRunResponse:
        """Commit a RENEW gate and run the forward step.

        Saga must be at RECEIVED. The loan extension is recorded on the
        ledger event; state stays RECEIVED. A ReShare renewal intent is
        enqueued via the outbox (sandbox-blocked on the HTTP client —
        surfaces as a dead-letter row; mock client succeeds in tests).
        """
        try:
            async with session.begin():
                coord = Coordinator(session=session, registry=registry)
                ledger = SagaLedger(session)

                saga = await ledger.get_saga(saga_id)
                current = LifecycleState(saga.current_state)
                if current != LifecycleState.RECEIVED:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"renew requires saga in 'received' state; "
                            f"current state is {current.value!r}"
                        ),
                    )

                await coord.commit_gate(
                    saga_id=saga_id,
                    step=StepName.RENEW,
                    actor=body.actor,
                    rationale=body.rationale,
                )

                events = await ledger.events_for(saga_id)
                extras = _derive_extras(
                    events,
                    {"extension_days": body.extension_days},
                )
                request = IllRequest.model_validate(saga.request_payload)
                ev = await coord.run_forward(
                    ctx=_make_context(
                        saga_id=saga_id,
                        request=request,
                        current_state=current,
                        actor=body.actor,
                        step=StepName.RENEW,
                        extras=extras,
                    ),
                    step=StepName.RENEW,
                )
            return StepRunResponse.model_validate(ev.model_dump())
        except SagaNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GateRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TerminalStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
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
                        # Deterministic key — see HTML compensator
                        # endpoint for rationale (audit #5).
                        idempotency_key=f"comp-{step.value}-{saga_id}",
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

    # Valid target states for the override endpoint.
    _OVERRIDE_TARGETS: frozenset[LifecycleState] = frozenset(
        {LifecycleState.CANCELLED, LifecycleState.UNFILLED}
    )

    @app.post(
        "/sagas/{saga_id}/override",
        response_model=StepRunResponse,
        status_code=status.HTTP_200_OK,
    )
    async def override(
        saga_id: UUID,
        body: OverrideBody,
        session: AsyncSession = Depends(_get_session),
    ) -> StepRunResponse:
        """Resolve a DISPUTED saga by force-setting it to CANCELLED or UNFILLED.

        Writes an OBSERVATION event (``step=resolve``, ``outcome=committed``)
        directly to the ledger, advancing ``saga.current_state`` atomically.
        No outbox dispatch occurs — any open ILS loans must be settled
        out-of-band by staff.

        Returns 404 if the saga does not exist, 409 if the saga is not in
        DISPUTED state, and 400 if ``target_state`` is not an allowed value.
        """
        try:
            target = LifecycleState(body.target_state)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"invalid target_state {body.target_state!r}; "
                    f"allowed: {sorted(s.value for s in _OVERRIDE_TARGETS)}"
                ),
            ) from exc

        if target not in _OVERRIDE_TARGETS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"target_state {target.value!r} is not allowed; "
                    f"allowed: {sorted(s.value for s in _OVERRIDE_TARGETS)}"
                ),
            )

        async with session.begin():
            ledger = SagaLedger(session)
            try:
                saga = await ledger.get_saga(saga_id)
            except SagaNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            current = LifecycleState(saga.current_state)
            if current != LifecycleState.DISPUTED:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"saga {saga_id} is in state {current.value!r}; "
                        "override only applies to sagas in 'disputed' state"
                    ),
                )

            ev = await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.OBSERVATION,
                    step=StepName.RESOLVE,
                    state_before=current,
                    state_after=target,
                    actor=body.actor,
                    idempotency_key=new_idempotency_key("override"),
                    outcome=StepOutcome.COMMITTED,
                    rationale=body.rationale,
                    payload={"target_state": target.value},
                )
            )
        return StepRunResponse.model_validate(ev.model_dump())

    # ----------------------------------------------------------------- patron portal
    # Read-only patron-facing views. Privacy posture for the prototype:
    # the saga UUID is the only access secret. The ``patron_id`` query
    # parameter is a UX label echoed into the page, not an access gate
    # (PR #134 dropped the patron-id 404 on the detail view since
    # ``/portal/requests?patron_id=...`` accepts arbitrary IDs anyway —
    # gating one without the other was false reassurance). Production
    # needs real patron auth, see ADR-0007.

    @app.get("/portal", response_class=HTMLResponse, include_in_schema=False)
    async def portal_home(request: Request) -> HTMLResponse:
        """Patron portal landing page — patron ID lookup form."""
        return templates.TemplateResponse(request, "portal_home.html", {})

    @app.get("/portal/requests", response_class=HTMLResponse, include_in_schema=False)
    async def portal_requests(
        request: Request,
        patron_id: str = Query(..., min_length=1),
        session: AsyncSession = Depends(_get_session),
    ) -> HTMLResponse:
        """List all sagas belonging to this patron ID.

        Filters via JSON-path WHERE clause so the cap is the **patron's**
        most recent 200 sagas, not the table's. Pre-fix took most-recent
        200 table-wide and filtered in Python — patrons whose sagas fell
        outside that window saw an empty list (false negative). Portable
        across Postgres JSONB and SQLite JSON via ``_json_type``.
        """
        async with session.begin():
            stmt = (
                select(Saga)
                .where(Saga.request_payload["patron"]["patron_id"].astext == patron_id)
                .order_by(Saga.updated_at.desc())
                .limit(200)
            )
            rows = (await session.execute(stmt)).scalars().all()

        patron_rows = []
        for saga in rows:
            raw = saga.request_payload or {}
            item = raw.get("item") or {}
            requesting = raw.get("requesting_library") or {}
            state = saga.current_state
            try:
                is_terminal = LifecycleState(state) in TERMINAL_STATES
            except ValueError:
                is_terminal = False
            patron_rows.append(
                {
                    "saga_id": str(saga.id),
                    "title": str(item.get("title") or ""),
                    "current_state": state,
                    "is_terminal": is_terminal,
                    "requesting_library": str(requesting.get("symbol") or ""),
                    "submitted_at": saga.created_at.strftime("%Y-%m-%d") if saga.created_at else "",
                }
            )

        return templates.TemplateResponse(
            request,
            "portal_requests.html",
            {"patron_id": patron_id, "requests": patron_rows},
        )

    @app.get(
        "/portal/requests/{saga_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def portal_saga_detail(
        saga_id: UUID,
        request: Request,
        patron_id: str = Query(..., min_length=1),
        session: AsyncSession = Depends(_get_session),
    ) -> HTMLResponse:
        """Read-only patron view of a single saga.

        Privacy posture for the prototype: the **saga UUID is the secret
        token**. There is no patron auth, so any caller who knows the
        saga-id may view it (typically reached via a private link). The
        ``patron_id`` query param is a UX label echoed into the page —
        not an access gate. Gating on patron-id while ``/portal/requests``
        accepts arbitrary IDs would be false reassurance: an attacker who
        can enumerate one can enumerate the other. See PRD-05 § Patron
        portal for the full prototype-limits writeup.

        Returns 404 only when the saga does not exist.
        """
        async with session.begin():
            saga = await session.get(Saga, saga_id)
            if saga is None:
                raise HTTPException(status_code=404, detail="request not found")
            raw = saga.request_payload or {}

            ledger = SagaLedger(session)
            events = await ledger.events_for(saga_id)

        item_raw = raw.get("item") or {}
        requesting = raw.get("requesting_library") or {}
        state = saga.current_state
        try:
            is_terminal = LifecycleState(state) in TERMINAL_STATES
        except ValueError:
            is_terminal = False

        due_date = _portal_due_date(events)
        renewals = sum(
            1 for ev in events
            if ev.kind == EventKind.FORWARD
            and ev.step.value == "renew"
            and ev.outcome == StepOutcome.COMMITTED
        )

        event_rows = []
        for ev in events:
            label = _patron_event_label(ev)
            if label is None:
                continue
            event_rows.append(
                {
                    "label": label,
                    "outcome": ev.outcome.value,
                    "ts": ev.ts.strftime("%Y-%m-%d %H:%M UTC") if ev.ts else "",
                }
            )

        return templates.TemplateResponse(
            request,
            "portal_detail.html",
            {
                "patron_id": patron_id,
                "item": {
                    "title": str(item_raw.get("title") or ""),
                    "author": str(item_raw.get("author") or ""),
                    "isbn": str(item_raw.get("isbn") or ""),
                },
                "current_state": state,
                "is_terminal": is_terminal,
                "requesting_library": str(requesting.get("symbol") or ""),
                "submitted_at": saga.created_at.strftime("%Y-%m-%d") if saga.created_at else "",
                "due_date": due_date,
                "renewals": renewals or "",
                "events": event_rows,
            },
        )

    return app


# Module-level instance for ``uvicorn agora.api.app:app``.
app = create_app()
