"""FastAPI application factory + routes.

The console exposes a small surface:
- ``/health``                       liveness/diagnostic
- ``POST /requests``                patron-side submit
- ``GET /sagas``                    list pending and active sagas
- ``GET /sagas/{id}``               full event timeline
- ``POST /sagas/{id}/approve``      commit gate (staff approves a step)
- ``POST /sagas/{id}/reject``       cancel pending gate
- ``POST /sagas/{id}/compensate``   manually trigger compensator

Auth is intentionally *not* implemented in the prototype — see ADR-0007.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agora import __version__
from agora.api.schemas import (
    ApprovalBody,
    CompensateBody,
    HealthResponse,
    RejectionBody,
    SagaDetail,
    SagaEventOut,
    SagaSummary,
    SubmitRequestResponse,
)
from agora.config import get_settings
from agora.logging import configure_logging, get_logger
from agora.models.events import NewSagaEvent
from agora.models.lifecycle import EventKind, LifecycleState, StepName, StepOutcome
from agora.models.request import IllRequest
from agora.saga.coordinator import Coordinator
from agora.saga.db import Saga, get_sessionmaker
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger

log = get_logger(__name__)


def create_app() -> FastAPI:
    """Build the FastAPI app. One per process is sufficient."""
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="Agora ILL",
        description="Agentic Inter-Library Loan staff console",
        version=__version__,
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

    @app.post("/sagas/{saga_id}/approve", status_code=204)
    async def approve(
        saga_id: UUID,
        body: ApprovalBody,
        session: AsyncSession = Depends(_get_session),
    ) -> None:
        step = _parse_step(body.step)
        async with session.begin():
            coord = Coordinator(session=session)
            await coord.commit_gate(
                saga_id=saga_id,
                step=step,
                actor=body.actor,
                rationale=body.rationale,
            )

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

    @app.post("/sagas/{saga_id}/compensate", status_code=204)
    async def compensate(
        saga_id: UUID,
        body: CompensateBody,
        session: AsyncSession = Depends(_get_session),
    ) -> None:
        # Compensator wiring requires a TransactionAgent + step registry,
        # which the demo script provides. The HTTP route returns a clear
        # "not wired in API" error for now.
        raise HTTPException(
            status_code=501,
            detail=(
                "Compensation not exposed via HTTP in the prototype. "
                "Use the demo CLI or instantiate Coordinator directly."
            ),
        )

    return app


# Module-level instance for ``uvicorn agora.api.app:app``.
app = create_app()


async def _get_session() -> AsyncIterator[AsyncSession]:  # FastAPI dependency
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session


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


def _parse_step(name: str) -> StepName:
    try:
        return StepName(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unknown step {name!r}") from exc
