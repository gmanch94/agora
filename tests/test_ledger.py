"""Saga ledger semantics."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agora.models.events import NewSagaEvent
from agora.models.lifecycle import (
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger, TerminalStateError


@pytest.mark.asyncio
async def test_create_saga_and_append_first_event(session: AsyncSession) -> None:
    saga_id = uuid4()
    request_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request_id,
            request_payload={},
        )
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.FORWARD,
                step=StepName.SUBMIT,
                state_before=LifecycleState.SUBMITTED,
                state_after=LifecycleState.SUBMITTED,
                actor="patron",
                idempotency_key=new_idempotency_key(),
                payload={},
                outcome=StepOutcome.COMMITTED,
            )
        )

    async with session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    assert len(events) == 1
    assert events[0].seq == 1
    assert events[0].step == StepName.SUBMIT


@pytest.mark.asyncio
async def test_replay_with_same_idempotency_key_is_noop(session: AsyncSession) -> None:
    saga_id = uuid4()
    key = new_idempotency_key()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(saga_id=saga_id, request_id=uuid4(), request_payload={})
        ev = NewSagaEvent(
            saga_id=saga_id,
            kind=EventKind.FORWARD,
            step=StepName.SUBMIT,
            state_before=LifecycleState.SUBMITTED,
            state_after=LifecycleState.SUBMITTED,
            actor="patron",
            idempotency_key=key,
            payload={"v": 1},
            outcome=StepOutcome.COMMITTED,
        )
        await ledger.append(ev)
        # Replay
        await ledger.append(ev)

    async with session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    assert len(events) == 1, "replay must not append duplicate event"


@pytest.mark.asyncio
async def test_state_advances_on_committed_forward(session: AsyncSession) -> None:
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(saga_id=saga_id, request_id=uuid4(), request_payload={})
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.FORWARD,
                step=StepName.ROUTE,
                state_before=LifecycleState.SUBMITTED,
                state_after=LifecycleState.ROUTED,
                actor="agent:routing",
                idempotency_key=new_idempotency_key(),
                payload={},
                outcome=StepOutcome.COMMITTED,
            )
        )

    async with session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
    assert saga.current_state == LifecycleState.ROUTED.value


@pytest.mark.asyncio
async def test_terminal_saga_blocks_further_state_change(session: AsyncSession) -> None:
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=uuid4(),
            request_payload={},
            initial_state=LifecycleState.RETURNED,
        )

    async with session.begin():
        ledger = SagaLedger(session)
        with pytest.raises(TerminalStateError):
            await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.FORWARD,
                    step=StepName.SHIP,
                    state_before=LifecycleState.RETURNED,
                    state_after=LifecycleState.SHIPPED,
                    actor="agent:transaction",
                    idempotency_key=new_idempotency_key(),
                    payload={},
                    outcome=StepOutcome.COMMITTED,
                )
            )


@pytest.mark.asyncio
async def test_find_committed_forward_returns_latest(session: AsyncSession) -> None:
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(saga_id=saga_id, request_id=uuid4(), request_payload={})
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.FORWARD,
                step=StepName.APPROVE,
                state_before=LifecycleState.ROUTED,
                state_after=LifecycleState.APPROVED,
                actor="agent:transaction",
                idempotency_key=new_idempotency_key(),
                payload={"reshare_id": "rs-1"},
                outcome=StepOutcome.COMMITTED,
            )
        )

    async with session.begin():
        ledger = SagaLedger(session)
        ev = await ledger.find_committed_forward(saga_id, "approve")
    assert ev is not None
    assert ev.payload == {"reshare_id": "rs-1"}
