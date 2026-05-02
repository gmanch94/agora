"""Saga coordinator end-to-end behaviour."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agora.agents.transaction import TransactionAgent
from agora.clients.reshare import MockReShareClient
from agora.models.events import NewSagaEvent
from agora.models.lifecycle import (
    EventKind,
    LifecycleState,
    StepName,
    StepOutcome,
)
from agora.models.request import (
    Citation,
    IllRequest,
    ItemMetadata,
    LibraryRef,
    PatronRef,
    RequestType,
)
from agora.saga.context import SagaContext
from agora.saga.coordinator import Coordinator, GateRequiredError
from agora.saga.flows import build_registry
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger


def _build_request() -> IllRequest:
    return IllRequest(
        request_type=RequestType.LOAN,
        patron=PatronRef(library_symbol="A", patron_id="p1"),
        requesting_library=LibraryRef(symbol="A"),
        item=ItemMetadata(title="Brave New World", author="Huxley", isbn="9780060850524"),
        citation=Citation(
            raw="ctx_ver=Z39.88-2004",
            parsed_from="openurl",
            parsed_at=datetime.now(UTC),
        ),
    )


@pytest.mark.asyncio
async def test_forward_step_blocked_without_committed_gate(session) -> None:
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))  # type: ignore[arg-type]

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
        )

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=LifecycleState.SUBMITTED,
            idempotency_key=new_idempotency_key(),
            actor="agent:transaction",
            extras={"chosen_supplier": "B"},
        )
        with pytest.raises(GateRequiredError):
            await coord.run_forward(ctx=ctx, step=StepName.ROUTE)


@pytest.mark.asyncio
async def test_happy_path_full_lifecycle(session) -> None:
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))  # type: ignore[arg-type]

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
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

    extras: dict = {"chosen_supplier": "B"}

    # ROUTE
    await _gate_and_run(session, registry, saga_id, request, StepName.ROUTE, extras)

    # APPROVE — captures reshare_id from forward result
    forward_event = await _gate_and_run(
        session, registry, saga_id, request, StepName.APPROVE, extras
    )
    extras["reshare_id"] = forward_event.payload["reshare_id"]

    # SHIP
    await _gate_and_run(session, registry, saga_id, request, StepName.SHIP, extras)

    # RETURN
    await _gate_and_run(
        session, registry, saga_id, request, StepName.RETURN_ITEM, extras
    )

    async with session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
    assert saga.current_state == LifecycleState.RETURNED.value


@pytest.mark.asyncio
async def test_compensator_on_approve_cancels_at_supplier(session) -> None:
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))  # type: ignore[arg-type]

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.ROUTED,
        )

    extras: dict = {"chosen_supplier": "B"}
    forward = await _gate_and_run(
        session,
        registry,
        saga_id,
        request,
        StepName.APPROVE,
        extras,
        from_state=LifecycleState.ROUTED,
    )
    assert forward.payload["reshare_id"]

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=LifecycleState.APPROVED,
            idempotency_key=new_idempotency_key(prefix="comp"),
            actor="agent:reconciliation",
        )
        await coord.run_compensator(ctx=ctx, step=StepName.APPROVE)

    async with session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
    assert saga.current_state == LifecycleState.CANCELLED.value


async def _gate_and_run(
    session,
    registry,
    saga_id,
    request,
    step: StepName,
    extras: dict,
    *,
    from_state: LifecycleState | None = None,
):
    """Open + commit a gate, then execute the forward step."""
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.open_gate(saga_id=saga_id, step=step, actor="staff:test")
        await coord.commit_gate(
            saga_id=saga_id,
            step=step,
            actor="staff:test",
            rationale=f"approve {step.value}",
        )

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=from_state or LifecycleState(saga.current_state),
            idempotency_key=new_idempotency_key(prefix=step.value),
            actor="agent:transaction",
            extras=dict(extras),
        )
        return await coord.run_forward(ctx=ctx, step=step)
