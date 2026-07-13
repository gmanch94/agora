"""State-transition enforcement in the coordinator + ledger.

Covers the 2026-07-13 code-review fixes:

1. ``run_forward`` validates the saga's persisted ``current_state``
   against ``FORWARD_STEP_ALLOWED_STATES`` (double-approve and
   step-skipping raise ``IllegalTransitionError``); gates are
   single-use (consumed by any later FORWARD event for the step).
2. ``run_compensator`` validates against ``COMPENSATOR_ALLOWED_STATES``
   (compensating SUBMIT at SHIPPED no longer terminal-cancels a saga
   with a live supplier loan).
4. FAILED forward events persist under ``{key}:failed`` so a retry
   with the original key succeeds; the ledger's idempotency identity
   check includes ``outcome``.
6. The ledger's terminal-state guard applies to ANY state-changing
   event kind (a state-changing OBSERVATION can no longer resurrect a
   terminal saga), while the APPROVING -> APPROVED worker projection
   and the staff DISPUTED -> CANCELLED/UNFILLED override keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from agora.saga.coordinator import (
    Coordinator,
    GateRequiredError,
    IllegalTransitionError,
)
from agora.saga.db import OutboxRow
from agora.saga.flows import build_registry
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import (
    IdempotencyConflictError,
    SagaLedger,
    TerminalStateError,
)
from agora.saga.steps import StepRegistry


def _build_request() -> IllRequest:
    return IllRequest(
        request_type=RequestType.LOAN,
        patron=PatronRef(library_symbol="A", patron_id="p1"),
        requesting_library=LibraryRef(symbol="A"),
        item=ItemMetadata(title="t", author="a", isbn="9780000000000"),
        citation=Citation(
            raw="r", parsed_from="openurl", parsed_at=datetime.now(UTC)
        ),
    )


async def _create_saga(
    session: AsyncSession,
    saga_id: UUID,
    request: IllRequest,
    initial_state: LifecycleState = LifecycleState.SUBMITTED,
) -> None:
    async with session.begin():
        await SagaLedger(session).create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=initial_state,
        )


async def _gate(
    session: AsyncSession,
    registry: StepRegistry,
    saga_id: UUID,
    step: StepName,
) -> None:
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.open_gate(saga_id=saga_id, step=step, actor="staff:t")
        await coord.commit_gate(
            saga_id=saga_id, step=step, actor="staff:t", rationale="ok"
        )


def _ctx(
    saga_id: UUID,
    request: IllRequest,
    state: LifecycleState,
    extras: dict[str, Any],
    *,
    key: str | None = None,
    prefix: str = "t",
) -> SagaContext:
    return SagaContext(
        saga_id=saga_id,
        request=request,
        current_state=state,
        idempotency_key=key or new_idempotency_key(prefix=prefix),
        actor="staff:t",
        extras=dict(extras),
    )


# ---------------------------------------------------------------------
# Fix 1a — forward state-transition validation
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_approve_raises_illegal_transition(
    session: AsyncSession,
) -> None:
    """Second APPROVE (fresh gate, fresh key) must not create a second
    supplier request — the saga is already APPROVING."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request, LifecycleState.ROUTED)

    extras = {"chosen_supplier": "B"}
    await _gate(session, registry, saga_id, StepName.APPROVE)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ev = await coord.run_forward(
            ctx=_ctx(saga_id, request, LifecycleState.ROUTED, extras),
            step=StepName.APPROVE,
        )
    assert ev.state_after == LifecycleState.APPROVING

    # Double POST: staff re-commits a gate and fires APPROVE again with
    # a fresh idempotency key. The state guard must refuse.
    await _gate(session, registry, saga_id, StepName.APPROVE)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(IllegalTransitionError) as exc_info:
            await coord.run_forward(
                ctx=_ctx(saga_id, request, LifecycleState.APPROVING, extras),
                step=StepName.APPROVE,
            )
    assert exc_info.value.step == StepName.APPROVE
    assert exc_info.value.current_state == LifecycleState.APPROVING

    # Exactly one send_request outbox row — no duplicate supplier call.
    async with session.begin():
        rows = (
            (
                await session.execute(
                    select(OutboxRow).where(OutboxRow.saga_id == saga_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_step_skipping_raises_illegal_transition(
    session: AsyncSession,
) -> None:
    """RECEIVE at APPROVED (SHIP skipped) must be refused."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request, LifecycleState.APPROVED)

    await _gate(session, registry, saga_id, StepName.RECEIVE)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(IllegalTransitionError):
            await coord.run_forward(
                ctx=_ctx(
                    saga_id,
                    request,
                    LifecycleState.APPROVED,
                    {"reshare_id": "rs-skip-1"},
                ),
                step=StepName.RECEIVE,
            )


@pytest.mark.asyncio
async def test_forward_step_without_transition_entry_fails_closed(
    session: AsyncSession,
) -> None:
    """Steps absent from FORWARD_STEP_ALLOWED_STATES (e.g. RESOLVE) are
    refused before touching the registry — default-deny."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request)

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(IllegalTransitionError):
            await coord.run_forward(
                ctx=_ctx(saga_id, request, LifecycleState.SUBMITTED, {}),
                step=StepName.RESOLVE,
                require_gate=False,
            )


# ---------------------------------------------------------------------
# Fix 1b — gates are single-use
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_is_consumed_by_forward(session: AsyncSession) -> None:
    """A committed gate spent by one forward must not authorize another.

    ROUTE forward -> ROUTE compensator (back to SUBMITTED) -> ROUTE
    forward again: the second forward needs a FRESH gate.
    """
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request)

    extras = {"chosen_supplier": "B"}
    await _gate(session, registry, saga_id, StepName.ROUTE)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.run_forward(
            ctx=_ctx(saga_id, request, LifecycleState.SUBMITTED, extras),
            step=StepName.ROUTE,
        )

    # Compensate ROUTE — saga returns to SUBMITTED for re-rank.
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.run_compensator(
            ctx=_ctx(saga_id, request, LifecycleState.ROUTED, extras),
            step=StepName.ROUTE,
        )

    # Re-running ROUTE without a fresh gate: the original gate was
    # consumed by the first forward.
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(GateRequiredError):
            await coord.run_forward(
                ctx=_ctx(saga_id, request, LifecycleState.SUBMITTED, extras),
                step=StepName.ROUTE,
            )

    # A fresh gate re-authorizes the step.
    await _gate(session, registry, saga_id, StepName.ROUTE)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ev = await coord.run_forward(
            ctx=_ctx(saga_id, request, LifecycleState.SUBMITTED, extras),
            step=StepName.ROUTE,
        )
    assert ev.state_after == LifecycleState.ROUTED


@pytest.mark.asyncio
async def test_failed_forward_consumes_gate_and_allows_keyed_retry(
    session: AsyncSession,
) -> None:
    """Fix 4 + 1b together: a forward failure persists a FAILED event
    under ``{key}:failed``; the same key retries cleanly after staff
    re-commit a gate (the FAILED forward consumed the first one)."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request, LifecycleState.APPROVED)

    fixed_key = new_idempotency_key(prefix="ship-retry")
    await _gate(session, registry, saga_id, StepName.SHIP)

    # First attempt fails (missing reshare_id). Catch INSIDE the
    # transaction so the FAILED event commits (mirrors a caller that
    # records the failure).
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(ValueError, match="reshare_id"):
            await coord.run_forward(
                ctx=_ctx(
                    saga_id, request, LifecycleState.APPROVED, {}, key=fixed_key
                ),
                step=StepName.SHIP,
            )

    async with session.begin():
        events = await SagaLedger(session).events_for(saga_id)
    failed = [
        e
        for e in events
        if e.kind == EventKind.FORWARD and e.outcome == StepOutcome.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].idempotency_key == f"{fixed_key}:failed"

    # No outbox intent rode on the failure.
    async with session.begin():
        rows = (
            (
                await session.execute(
                    select(OutboxRow).where(OutboxRow.saga_id == saga_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []

    # Retry with the SAME key but without a fresh gate: the FAILED
    # forward consumed the gate (default-deny — retry is a new staff
    # decision).
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(GateRequiredError):
            await coord.run_forward(
                ctx=_ctx(
                    saga_id,
                    request,
                    LifecycleState.APPROVED,
                    {"reshare_id": "rs-retry-1"},
                    key=fixed_key,
                ),
                step=StepName.SHIP,
            )

    # Fresh gate + same key + fixed extras -> COMMITTED under the bare
    # key (no collision with the FAILED row).
    await _gate(session, registry, saga_id, StepName.SHIP)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ev = await coord.run_forward(
            ctx=_ctx(
                saga_id,
                request,
                LifecycleState.APPROVED,
                {"reshare_id": "rs-retry-1"},
                key=fixed_key,
            ),
            step=StepName.SHIP,
        )
    assert ev.state_after == LifecycleState.SHIPPED
    assert ev.outcome == StepOutcome.COMMITTED
    assert ev.idempotency_key == fixed_key


@pytest.mark.asyncio
async def test_forward_replay_short_circuits_before_state_guard(
    session: AsyncSession,
) -> None:
    """Replaying a committed forward with its original key returns the
    persisted event (no IllegalTransitionError, no duplicate outbox)."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request, LifecycleState.APPROVED)

    fixed_key = new_idempotency_key(prefix="ship-replay")
    extras = {"reshare_id": "rs-replay-2"}
    await _gate(session, registry, saga_id, StepName.SHIP)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        first = await coord.run_forward(
            ctx=_ctx(
                saga_id, request, LifecycleState.APPROVED, extras, key=fixed_key
            ),
            step=StepName.SHIP,
        )

    # Saga is now SHIPPED — a naive re-run would trip the state guard.
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        replayed = await coord.run_forward(
            ctx=_ctx(
                saga_id, request, LifecycleState.SHIPPED, extras, key=fixed_key
            ),
            step=StepName.SHIP,
        )
    assert replayed.seq == first.seq
    assert replayed.idempotency_key == fixed_key

    async with session.begin():
        rows = (
            (
                await session.execute(
                    select(OutboxRow).where(OutboxRow.saga_id == saga_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "replay must not double-enqueue"


@pytest.mark.asyncio
async def test_forward_key_reuse_for_different_step_conflicts(
    session: AsyncSession,
) -> None:
    """A key already spent on one step cannot authorize another step."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request)

    fixed_key = new_idempotency_key(prefix="reuse")
    await _gate(session, registry, saga_id, StepName.ROUTE)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.run_forward(
            ctx=_ctx(
                saga_id,
                request,
                LifecycleState.SUBMITTED,
                {"chosen_supplier": "B"},
                key=fixed_key,
            ),
            step=StepName.ROUTE,
        )

    await _gate(session, registry, saga_id, StepName.APPROVE)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(IdempotencyConflictError):
            await coord.run_forward(
                ctx=_ctx(
                    saga_id,
                    request,
                    LifecycleState.ROUTED,
                    {"chosen_supplier": "B"},
                    key=fixed_key,  # reused across steps — must conflict
                ),
                step=StepName.APPROVE,
            )


# ---------------------------------------------------------------------
# Fix 2 — compensator state validation
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compensate_submit_at_shipped_is_refused(
    session: AsyncSession,
) -> None:
    """The review's stranded-loan scenario: compensate step=submit while
    the saga is SHIPPED would terminal-cancel with zero outbox intents,
    leaving the supplier-side loan live. Must raise."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request)

    # SUBMIT forward (initial step, gate-exempt) then walk the ledger
    # to SHIPPED via direct committed FORWARD events.
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.run_forward(
            ctx=_ctx(saga_id, request, LifecycleState.SUBMITTED, {}),
            step=StepName.SUBMIT,
            require_gate=False,
        )
    async with session.begin():
        ledger = SagaLedger(session)
        for step, before, after in [
            (StepName.ROUTE, LifecycleState.SUBMITTED, LifecycleState.ROUTED),
            (StepName.APPROVE, LifecycleState.ROUTED, LifecycleState.APPROVED),
            (StepName.SHIP, LifecycleState.APPROVED, LifecycleState.SHIPPED),
        ]:
            await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.FORWARD,
                    step=step,
                    state_before=before,
                    state_after=after,
                    actor="staff:t",
                    idempotency_key=new_idempotency_key(prefix=step.value),
                    payload={"reshare_id": "rs-live-1"},
                    outcome=StepOutcome.COMMITTED,
                )
            )

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(IllegalTransitionError) as exc_info:
            await coord.run_compensator(
                ctx=_ctx(saga_id, request, LifecycleState.SHIPPED, {}),
                step=StepName.SUBMIT,
            )
    assert exc_info.value.kind == "compensator"
    assert exc_info.value.current_state == LifecycleState.SHIPPED

    # Saga untouched — still SHIPPED, not CANCELLED.
    async with session.begin():
        saga = await SagaLedger(session).get_saga(saga_id)
    assert saga.current_state == LifecycleState.SHIPPED.value


@pytest.mark.asyncio
async def test_compensator_replay_with_deterministic_key_is_idempotent(
    session: AsyncSession,
) -> None:
    """Second /compensate with the deterministic ``comp-{step}-{saga}``
    key returns the prior event even though the saga has since moved to
    the comp target state (DISPUTED here)."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request, LifecycleState.APPROVED)

    extras = {"reshare_id": "rs-comp-replay"}
    await _gate(session, registry, saga_id, StepName.SHIP)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.run_forward(
            ctx=_ctx(saga_id, request, LifecycleState.APPROVED, extras),
            step=StepName.SHIP,
        )

    comp_key = f"comp-{StepName.SHIP.value}-{saga_id}"
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        first = await coord.run_compensator(
            ctx=_ctx(
                saga_id, request, LifecycleState.SHIPPED, extras, key=comp_key
            ),
            step=StepName.SHIP,
        )
    assert first.state_after == LifecycleState.DISPUTED

    # Saga is now DISPUTED (not in SHIP-comp's allowed states) — the
    # replay must short-circuit on the key, not raise.
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        second = await coord.run_compensator(
            ctx=_ctx(
                saga_id, request, LifecycleState.DISPUTED, extras, key=comp_key
            ),
            step=StepName.SHIP,
        )
    assert second.seq == first.seq

    async with session.begin():
        events = await SagaLedger(session).events_for(saga_id)
    comps = [e for e in events if e.kind == EventKind.COMPENSATOR]
    assert len(comps) == 1


@pytest.mark.asyncio
async def test_compensate_route_at_submitted_is_refused(
    session: AsyncSession,
) -> None:
    """ROUTE comp is only legal at ROUTED; running it after the saga
    already returned to SUBMITTED (fresh key) is refused."""
    saga_id = uuid4()
    request = _build_request()
    registry = build_registry(TransactionAgent(MockReShareClient()))
    await _create_saga(session, saga_id, request)

    extras = {"chosen_supplier": "B"}
    await _gate(session, registry, saga_id, StepName.ROUTE)
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.run_forward(
            ctx=_ctx(saga_id, request, LifecycleState.SUBMITTED, extras),
            step=StepName.ROUTE,
        )
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.run_compensator(
            ctx=_ctx(saga_id, request, LifecycleState.ROUTED, extras),
            step=StepName.ROUTE,
        )

    # Saga is back at SUBMITTED; a second comp with a FRESH key must
    # be refused (the deterministic-key replay path is separate).
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        with pytest.raises(IllegalTransitionError):
            await coord.run_compensator(
                ctx=_ctx(saga_id, request, LifecycleState.SUBMITTED, extras),
                step=StepName.ROUTE,
            )


# ---------------------------------------------------------------------
# Fix 4 — ledger identity check includes outcome
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_key_collision_with_different_outcome_conflicts(
    session: AsyncSession,
) -> None:
    """Same key, same (saga, step, kind), different outcome — the
    replay path must raise instead of returning the FAILED row as if
    it were committed."""
    saga_id = uuid4()
    key = new_idempotency_key(prefix="outcome-clash")
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id, request_id=uuid4(), request_payload={}
        )
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.FORWARD,
                step=StepName.SHIP,
                state_before=LifecycleState.APPROVED,
                state_after=LifecycleState.APPROVED,
                actor="staff:t",
                idempotency_key=key,
                payload={"error": "boom"},
                outcome=StepOutcome.FAILED,
            )
        )
        with pytest.raises(IdempotencyConflictError, match="outcome"):
            await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.FORWARD,
                    step=StepName.SHIP,
                    state_before=LifecycleState.APPROVED,
                    state_after=LifecycleState.SHIPPED,
                    actor="staff:t",
                    idempotency_key=key,
                    payload={},
                    outcome=StepOutcome.COMMITTED,
                )
            )


# ---------------------------------------------------------------------
# Fix 6 — terminal-state guard covers state-changing OBSERVATIONs
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_changing_observation_cannot_resurrect_terminal_saga(
    session: AsyncSession,
) -> None:
    """An OBSERVATION whose state_after differs from a terminal
    current_state must be refused (previously it slipped past the
    guard and the promotion block moved the saga out of terminal)."""
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=uuid4(),
            request_payload={},
            initial_state=LifecycleState.CANCELLED,
        )

    async with session.begin():
        ledger = SagaLedger(session)
        with pytest.raises(TerminalStateError):
            await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.OBSERVATION,
                    step=StepName.APPROVE,
                    state_before=LifecycleState.CANCELLED,
                    state_after=LifecycleState.APPROVED,
                    actor="agent:outbox-worker",
                    idempotency_key=new_idempotency_key(prefix="resurrect"),
                    payload={},
                    outcome=StepOutcome.COMMITTED,
                )
            )

    async with session.begin():
        saga = await SagaLedger(session).get_saga(saga_id)
    assert saga.current_state == LifecycleState.CANCELLED.value


@pytest.mark.asyncio
async def test_non_state_changing_observation_on_terminal_saga_allowed(
    session: AsyncSession,
) -> None:
    """Audit-trail OBSERVATIONs (state_after == current) on terminal
    sagas keep working — the worker projection's late-ack path."""
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=uuid4(),
            request_payload={},
            initial_state=LifecycleState.CANCELLED,
        )
        ev = await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.OBSERVATION,
                step=StepName.APPROVE,
                state_before=LifecycleState.CANCELLED,
                state_after=LifecycleState.CANCELLED,
                actor="agent:outbox-worker",
                idempotency_key=new_idempotency_key(prefix="late-ack"),
                payload={"reshare_id": "rs-late"},
                outcome=StepOutcome.COMMITTED,
            )
        )
    assert ev.state_after == LifecycleState.CANCELLED


@pytest.mark.asyncio
async def test_state_changing_observation_on_live_saga_still_promotes(
    session: AsyncSession,
) -> None:
    """The APPROVING -> APPROVED worker projection (a COMMITTED,
    state-changing OBSERVATION on a NON-terminal saga) must keep
    advancing the saga."""
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=uuid4(),
            request_payload={},
            initial_state=LifecycleState.APPROVING,
        )
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.OBSERVATION,
                step=StepName.APPROVE,
                state_before=LifecycleState.APPROVING,
                state_after=LifecycleState.APPROVED,
                actor="agent:outbox-worker",
                idempotency_key=new_idempotency_key(prefix="ack"),
                payload={"reshare_id": "rs-ack"},
                outcome=StepOutcome.COMMITTED,
            )
        )

    async with session.begin():
        saga = await SagaLedger(session).get_saga(saga_id)
    assert saga.current_state == LifecycleState.APPROVED.value


@pytest.mark.asyncio
async def test_staff_resolve_override_from_disputed_still_allowed(
    session: AsyncSession,
) -> None:
    """The POST /sagas/{id}/override path — a RESOLVE OBSERVATION moving
    DISPUTED to CANCELLED/UNFILLED — is the one sanctioned terminal ->
    terminal transition and must survive the tightened guard."""
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=uuid4(),
            request_payload={},
            initial_state=LifecycleState.DISPUTED,
        )
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.OBSERVATION,
                step=StepName.RESOLVE,
                state_before=LifecycleState.DISPUTED,
                state_after=LifecycleState.CANCELLED,
                actor="staff:t",
                idempotency_key=new_idempotency_key(prefix="override"),
                payload={"target_state": "cancelled"},
                outcome=StepOutcome.COMMITTED,
            )
        )

    async with session.begin():
        saga = await SagaLedger(session).get_saga(saga_id)
    assert saga.current_state == LifecycleState.CANCELLED.value


@pytest.mark.asyncio
async def test_resolve_override_cannot_target_live_state(
    session: AsyncSession,
) -> None:
    """A forged RESOLVE aiming DISPUTED at a NON-terminal state (e.g.
    APPROVED) is refused — the carve-out only spans terminal states."""
    saga_id = uuid4()
    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=uuid4(),
            request_payload={},
            initial_state=LifecycleState.DISPUTED,
        )

    async with session.begin():
        ledger = SagaLedger(session)
        with pytest.raises(TerminalStateError):
            await ledger.append(
                NewSagaEvent(
                    saga_id=saga_id,
                    kind=EventKind.OBSERVATION,
                    step=StepName.RESOLVE,
                    state_before=LifecycleState.DISPUTED,
                    state_after=LifecycleState.APPROVED,
                    actor="staff:t",
                    idempotency_key=new_idempotency_key(prefix="forged"),
                    payload={},
                    outcome=StepOutcome.COMMITTED,
                )
            )
