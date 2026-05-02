"""Event-sourced saga ledger.

The ledger is the system-of-record. Every state change for a saga is
appended as an immutable row in ``saga_event``. The current state of a
saga can always be reconstructed by replaying its events.

Replay-safety is enforced by the ``UNIQUE(idempotency_key)`` constraint:
appending the same event twice raises an integrity error, which the
ledger maps onto a soft "already recorded" return.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agora.models.events import NewSagaEvent, SagaEvent
from agora.models.lifecycle import (
    TERMINAL_STATES,
    EventKind,
    LifecycleState,
    StepOutcome,
)
from agora.saga.db import Saga, SagaEventRow


class SagaLedgerError(Exception):
    """Base error class for ledger operations."""


class SagaNotFoundError(SagaLedgerError):
    """Raised when a saga id is unknown."""


class TerminalStateError(SagaLedgerError):
    """Raised when attempting to append to a terminal saga."""


class SagaLedger:
    """Thin façade over saga + saga_event tables.

    All writes happen inside the caller's session/transaction so that
    saga ledger writes can be atomic with related outbox/inbox writes.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_saga(
        self,
        *,
        saga_id: UUID,
        request_id: UUID,
        request_payload: dict[str, Any],
        initial_state: LifecycleState = LifecycleState.SUBMITTED,
    ) -> Saga:
        """Insert a new saga row.

        The first ``saga_event`` (kind=forward, step=submit) should be
        appended in the same transaction by the caller; this method
        only writes the lightweight pointer row.
        """
        saga = Saga(
            id=saga_id,
            request_id=request_id,
            current_state=initial_state.value,
            request_payload=request_payload,
        )
        self._session.add(saga)
        await self._session.flush()
        return saga

    async def get_saga(self, saga_id: UUID) -> Saga:
        saga = await self._session.get(Saga, saga_id)
        if saga is None:
            raise SagaNotFoundError(f"saga {saga_id} not found")
        return saga

    async def append(self, event: NewSagaEvent) -> SagaEvent | None:
        """Append a new event row.

        Returns ``None`` if the idempotency key already exists (replay).
        Raises ``TerminalStateError`` if the saga is already terminal
        and the event is not a benign observation.
        """
        saga = await self.get_saga(event.saga_id)
        current = LifecycleState(saga.current_state)

        if (
            current in TERMINAL_STATES
            and event.kind != EventKind.OBSERVATION
            and event.state_after != current
        ):
            raise TerminalStateError(
                f"saga {event.saga_id} is terminal ({current.value}); "
                f"refusing event step={event.step.value} kind={event.kind.value}"
            )

        next_seq = await self._next_seq(event.saga_id)

        row = SagaEventRow(
            saga_id=event.saga_id,
            seq=next_seq,
            kind=event.kind.value,
            step=event.step.value,
            state_before=event.state_before.value,
            state_after=event.state_after.value,
            actor=event.actor,
            idempotency_key=event.idempotency_key,
            iso_message_id=event.iso_message_id,
            payload=event.payload,
            outcome=event.outcome.value,
            rationale=event.rationale,
        )

        # Use a savepoint so a unique-constraint conflict here does not
        # roll back the caller's outer transaction.
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            # Either idempotency_key collision (benign replay) or
            # (saga_id, seq) collision (concurrent writer). Look up by
            # idempotency key — if found, treat as replay and return
            # the existing row. Otherwise re-raise.
            existing = await self._find_by_idempotency(event.idempotency_key)
            if existing is not None:
                return existing
            raise

        # Promote saga.current_state when forward step commits.
        if event.outcome == StepOutcome.COMMITTED and event.state_after != current:
            saga.current_state = event.state_after.value
            await self._session.flush()

        return _to_pydantic(row)

    async def events_for(self, saga_id: UUID) -> list[SagaEvent]:
        stmt = (
            select(SagaEventRow)
            .where(SagaEventRow.saga_id == saga_id)
            .order_by(SagaEventRow.seq.asc())
        )
        result = await self._session.execute(stmt)
        return [_to_pydantic(r) for r in result.scalars().all()]

    async def find_committed_forward(
        self,
        saga_id: UUID,
        step: str,
    ) -> SagaEvent | None:
        """Find the most recent committed forward event for a step.

        Used by reconciliation to confirm a forward step happened before
        running its compensator.
        """
        stmt = (
            select(SagaEventRow)
            .where(SagaEventRow.saga_id == saga_id)
            .where(SagaEventRow.kind == EventKind.FORWARD.value)
            .where(SagaEventRow.step == step)
            .where(SagaEventRow.outcome == StepOutcome.COMMITTED.value)
            .order_by(SagaEventRow.seq.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_pydantic(row) if row is not None else None

    async def _next_seq(self, saga_id: UUID) -> int:
        stmt = select(func.coalesce(func.max(SagaEventRow.seq), 0)).where(
            SagaEventRow.saga_id == saga_id
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one()) + 1

    async def _find_by_idempotency(self, key: str) -> SagaEvent | None:
        stmt = select(SagaEventRow).where(SagaEventRow.idempotency_key == key).limit(1)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_pydantic(row) if row is not None else None


def _to_pydantic(row: SagaEventRow) -> SagaEvent:
    """Convert ORM row to read-side pydantic model."""
    return SagaEvent.model_validate(
        {
            "id": row.id,
            "saga_id": row.saga_id,
            "seq": row.seq,
            "kind": row.kind,
            "step": row.step,
            "state_before": row.state_before,
            "state_after": row.state_after,
            "actor": row.actor,
            "idempotency_key": row.idempotency_key,
            "iso_message_id": row.iso_message_id,
            "payload": row.payload,
            "outcome": row.outcome,
            "rationale": row.rationale,
            "ts": row.ts,
        }
    )
