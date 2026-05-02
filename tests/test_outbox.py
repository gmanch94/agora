"""Tests for the outbox worker.

Covers:
- happy-path enqueue → drain → delivered
- handler failure → attempts++ + backoff scheduled
- max_attempts → dead_letter
- duplicate idempotency_key → IntegrityError surfaces
- scheduled_for in future → not picked up
- ReShare handler integration: drained payload reaches MockReShareClient
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agora.clients.ncip import MockNcipClient
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
from agora.saga.db import OutboxRow
from agora.saga.idempotency import new_idempotency_key, outbox_enqueue
from agora.saga.ledger import SagaLedger
from agora.saga.outbox import (
    DrainStats,
    Handler,
    OutboxWorker,
    make_ncip_handler,
    make_reshare_handler,
    make_reshare_on_success,
)


@pytest.fixture
def sm(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def _enqueue(
    sm: async_sessionmaker[AsyncSession],
    *,
    target: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    scheduled_for: datetime | None = None,
) -> int:
    """Helper: enqueue a row, commit, return row id."""
    async with sm() as s:
        row = await outbox_enqueue(
            s,
            saga_id=uuid4(),
            target=target,
            idempotency_key=idempotency_key or f"idem-{uuid4()}",
            payload=payload,
            scheduled_for=scheduled_for,
        )
        await s.commit()
        return row.id


async def _row(sm: async_sessionmaker[AsyncSession], row_id: int) -> OutboxRow:
    async with sm() as s:
        result = await s.execute(select(OutboxRow).where(OutboxRow.id == row_id))
        row = result.scalar_one()
        return row


async def test_drain_marks_delivered(sm: async_sessionmaker[AsyncSession]) -> None:
    seen: list[tuple[dict[str, Any], str]] = []

    async def ok_handler(payload: dict[str, Any], idem: str) -> None:
        seen.append((payload, idem))

    row_id = await _enqueue(
        sm,
        target="t1",
        payload={"hello": "world"},
        idempotency_key="idem-ok-1",
    )

    worker = OutboxWorker(sm, {"t1": ok_handler})
    stats = await worker.drain_once()

    assert stats == DrainStats(delivered=1)
    assert seen == [({"hello": "world"}, "idem-ok-1")]

    row = await _row(sm, row_id)
    assert row.status == "delivered"
    assert row.delivered_at is not None
    assert row.attempts == 0  # no retries needed


async def test_handler_failure_marks_pending_with_backoff(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    async def boom(payload: dict[str, Any], idem: str) -> None:
        raise RuntimeError("dispatch broke")

    row_id = await _enqueue(sm, target="t1", payload={})
    before = datetime.now(UTC)

    worker = OutboxWorker(sm, {"t1": boom}, base_backoff_secs=30)
    stats = await worker.drain_once()

    assert stats == DrainStats(failed=1)
    row = await _row(sm, row_id)
    assert row.status == "pending"  # still retriable
    assert row.attempts == 1
    assert row.last_error is not None and "dispatch broke" in row.last_error
    assert row.delivered_at is None
    # backoff = 30 * 2**0 = 30s; scheduled_for is offset-aware, before may
    # be returned as offset-naive on SQLite — compare both as UTC.
    sched = row.scheduled_for
    if sched.tzinfo is None:
        sched = sched.replace(tzinfo=UTC)
    assert sched >= before + timedelta(seconds=29)


async def test_max_attempts_marks_dead_letter(sm: async_sessionmaker[AsyncSession]) -> None:
    async def always_fail(payload: dict[str, Any], idem: str) -> None:
        raise RuntimeError("nope")

    row_id = await _enqueue(sm, target="t1", payload={})

    # max_attempts=3 + base_backoff_secs=0 so each retry is immediately
    # eligible for the next pass.
    worker = OutboxWorker(
        sm, {"t1": always_fail}, max_attempts=3, base_backoff_secs=0
    )
    stats = await worker.drain_until_empty()

    assert stats.dead_letter == 1
    assert stats.failed == 2  # 2 retriable failures + 1 terminal
    row = await _row(sm, row_id)
    assert row.status == "dead_letter"
    assert row.attempts == 3


async def test_duplicate_idempotency_key_raises(sm: async_sessionmaker[AsyncSession]) -> None:
    """Two enqueues with the same idem key must hit the UNIQUE constraint."""
    key = f"idem-dup-{uuid4()}"
    await _enqueue(sm, target="t1", payload={}, idempotency_key=key)

    with pytest.raises(IntegrityError):
        await _enqueue(sm, target="t1", payload={}, idempotency_key=key)


async def test_scheduled_for_future_is_skipped(sm: async_sessionmaker[AsyncSession]) -> None:
    seen: list[str] = []

    async def handler(payload: dict[str, Any], idem: str) -> None:
        seen.append(idem)

    future = datetime.now(UTC) + timedelta(hours=1)
    row_id = await _enqueue(
        sm,
        target="t1",
        payload={},
        idempotency_key="idem-future",
        scheduled_for=future,
    )

    worker = OutboxWorker(sm, {"t1": handler})
    stats = await worker.drain_once()

    assert stats == DrainStats()  # nothing drained
    assert seen == []
    row = await _row(sm, row_id)
    assert row.status == "pending"


async def _seed_saga(
    sm: async_sessionmaker[AsyncSession],
    *,
    saga_id: Any,
    initial_state: LifecycleState = LifecycleState.APPROVING,
) -> IllRequest:
    """Insert a saga row + its SUBMIT forward + a synthesised APPROVE forward.

    Used by projection tests so the OBSERVATION the worker writes has
    a parent saga and prior FORWARD event to attach to. APPROVE
    forward is required for ``find_committed_forward(APPROVE)`` to
    return non-None during reconciliation.
    """
    request = IllRequest(
        request_type=RequestType.LOAN,
        patron=PatronRef(library_symbol="A", patron_id="p1"),
        requesting_library=LibraryRef(symbol="A"),
        item=ItemMetadata(title="t", author="a", isbn="9780000000000"),
        citation=Citation(
            raw="r", parsed_from="openurl", parsed_at=datetime.now(UTC)
        ),
    )
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=initial_state,
        )
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.FORWARD,
                step=StepName.APPROVE,
                state_before=LifecycleState.ROUTED,
                state_after=initial_state,
                actor="agent:transaction",
                idempotency_key=new_idempotency_key(prefix="approve-seed"),
                payload={"supplier_symbol": "MEMBER1"},
                outcome=StepOutcome.COMMITTED,
            )
        )
    return request


async def test_on_success_projection_invoked_with_handler_result(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """``on_success`` receives ``(session, row_id, saga_id, payload, idem, result)``.

    Verifies the worker forwards the handler's return value verbatim
    and threads the snapshotted row identity through.
    """
    saga_id = uuid4()
    await _seed_saga(sm, saga_id=saga_id)

    captured: dict[str, Any] = {}

    async def handler(payload: dict[str, Any], idempotency_key: str) -> str:
        return f"result-for-{idempotency_key}"

    async def projection(
        session: AsyncSession,
        row_id: int,
        sid: Any,
        payload: dict[str, Any],
        idempotency_key: str,
        result: Any,
    ) -> None:
        captured.update(
            row_id=row_id,
            saga_id=sid,
            payload=payload,
            idempotency_key=idempotency_key,
            result=result,
        )

    row_id = await _enqueue(
        sm,
        target="t1",
        payload={"action": "demo", "args": {}},
        idempotency_key="idem-proj-1",
    )
    # Override saga_id on the just-enqueued row so the projection
    # sees the seeded saga (the helper enqueues with a fresh uuid4).
    async with sm() as s, s.begin():
        row = (
            await s.execute(select(OutboxRow).where(OutboxRow.id == row_id))
        ).scalar_one()
        row.saga_id = saga_id

    worker = OutboxWorker(
        sm,
        handlers={"t1": handler},
        on_success={"t1": projection},
    )
    stats = await worker.drain_once()
    assert stats == DrainStats(delivered=1)

    assert captured["row_id"] == row_id
    assert captured["saga_id"] == saga_id
    assert captured["payload"] == {"action": "demo", "args": {}}
    assert captured["idempotency_key"] == "idem-proj-1"
    assert captured["result"] == "result-for-idem-proj-1"


async def test_on_success_projection_failure_keeps_row_pending(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """If ``on_success`` raises, ``mark_delivered`` must not commit.

    The atomicity guarantee: the projection write and the delivered
    flag share one session/commit. A failing projection means the
    row stays pending, the next drain pass replays both.
    """
    handled: list[str] = []

    async def handler(payload: dict[str, Any], idempotency_key: str) -> str:
        handled.append(idempotency_key)
        return "ok"

    async def projection(
        session: AsyncSession,
        row_id: int,
        sid: Any,
        payload: dict[str, Any],
        idempotency_key: str,
        result: Any,
    ) -> None:
        raise RuntimeError("simulated DB blip writing projection")

    row_id = await _enqueue(
        sm,
        target="t1",
        payload={"action": "demo", "args": {}},
        idempotency_key="idem-proj-fail",
    )
    worker = OutboxWorker(
        sm,
        handlers={"t1": handler},
        on_success={"t1": projection},
        base_backoff_secs=0,
    )
    stats = await worker.drain_once()

    # Handler fired (returns "ok"); projection raised; row counted
    # as failed and remains pending for retry.
    assert handled == ["idem-proj-fail"]
    assert stats.failed == 1
    assert stats.delivered == 0

    row = await _row(sm, row_id)
    assert row.status == "pending"
    assert row.attempts == 1
    assert "projection failed" in (row.last_error or "")
    assert "simulated DB blip" in (row.last_error or "")


async def test_reshare_send_request_projects_approve_observation(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """Integration: real handler + real projection round-trips reshare_id.

    Mirrors the lifespan wiring (``api._build_outbox_worker``). On a
    successful ``send_request`` drain, the saga must transition
    APPROVING → APPROVED and a single APPROVE OBSERVATION must
    carry the supplier-assigned ``reshare_id``.
    """
    client = MockReShareClient()
    saga_id = uuid4()
    await _seed_saga(sm, saga_id=saga_id, initial_state=LifecycleState.APPROVING)

    row_id = await _enqueue(
        sm,
        target="reshare",
        payload={
            "action": "send_request",
            "args": {
                "request_payload": {"request_id": str(uuid4())},
                "supplier_symbol": "MEMBER1",
            },
        },
        idempotency_key="reshare-proj-1",
    )
    # Re-point the row at our seeded saga.
    async with sm() as s, s.begin():
        row = (
            await s.execute(select(OutboxRow).where(OutboxRow.id == row_id))
        ).scalar_one()
        row.saga_id = saga_id

    worker = OutboxWorker(
        sm,
        handlers={"reshare": make_reshare_handler(client)},
        on_success={"reshare": make_reshare_on_success()},
    )
    stats = await worker.drain_once()
    assert stats == DrainStats(delivered=1)

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
        events = await ledger.events_for(saga_id)

    assert saga.current_state == LifecycleState.APPROVED.value
    observations = [
        e for e in events
        if e.kind == EventKind.OBSERVATION and e.step == StepName.APPROVE
    ]
    assert len(observations) == 1
    obs = observations[0]
    assert obs.state_before == LifecycleState.APPROVING
    assert obs.state_after == LifecycleState.APPROVED
    assert obs.payload["reshare_id"].startswith("rs-")
    assert obs.payload["supplier_symbol"] == "MEMBER1"
    assert obs.idempotency_key == f"approve-ack-{row_id}"
    assert obs.iso_message_id is not None


async def test_reshare_projection_no_op_for_non_send_request_actions(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """``cancel_request`` / ``confirm_shipment`` etc. must NOT project.

    Only ``send_request`` carries supplier-assigned data the saga
    ledger consumes. Other actions return ``ReShareSendResult`` for
    consistency, but the projection skips them.
    """
    client = MockReShareClient()
    saga_id = uuid4()
    await _seed_saga(sm, saga_id=saga_id, initial_state=LifecycleState.APPROVED)

    # First land a real send_request so a reshare_id exists.
    init = await client.send_request(
        idempotency_key="seed-init",
        request_payload={"request_id": "seed"},
        supplier_symbol="MEMBER1",
    )

    row_id = await _enqueue(
        sm,
        target="reshare",
        payload={
            "action": "cancel_request",
            "args": {"reshare_id": init.reshare_id, "reason": "test"},
        },
        idempotency_key="cancel-proj",
    )
    async with sm() as s, s.begin():
        row = (
            await s.execute(select(OutboxRow).where(OutboxRow.id == row_id))
        ).scalar_one()
        row.saga_id = saga_id

    worker = OutboxWorker(
        sm,
        handlers={"reshare": make_reshare_handler(client)},
        on_success={"reshare": make_reshare_on_success()},
    )
    stats = await worker.drain_once()
    assert stats == DrainStats(delivered=1)

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
        events = await ledger.events_for(saga_id)

    # Saga state untouched; no APPROVE OBSERVATION written.
    assert saga.current_state == LifecycleState.APPROVED.value
    assert not any(
        e.kind == EventKind.OBSERVATION and e.step == StepName.APPROVE
        for e in events
    )


async def test_reshare_projection_skips_state_change_when_not_approving(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """If saga is not APPROVING (e.g. compensated to CANCELLED while the
    worker was mid-flight), record an audit OBSERVATION but do not
    flip the state away from CANCELLED.
    """
    client = MockReShareClient()
    saga_id = uuid4()
    await _seed_saga(sm, saga_id=saga_id, initial_state=LifecycleState.CANCELLED)

    row_id = await _enqueue(
        sm,
        target="reshare",
        payload={
            "action": "send_request",
            "args": {
                "request_payload": {"request_id": "x"},
                "supplier_symbol": "M",
            },
        },
        idempotency_key="reshare-late-1",
    )
    async with sm() as s, s.begin():
        row = (
            await s.execute(select(OutboxRow).where(OutboxRow.id == row_id))
        ).scalar_one()
        row.saga_id = saga_id

    worker = OutboxWorker(
        sm,
        handlers={"reshare": make_reshare_handler(client)},
        on_success={"reshare": make_reshare_on_success()},
    )
    stats = await worker.drain_once()
    assert stats == DrainStats(delivered=1)

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
        events = await ledger.events_for(saga_id)

    # Cancellation preserved; OBSERVATION recorded with no transition.
    assert saga.current_state == LifecycleState.CANCELLED.value
    obs = [
        e for e in events
        if e.kind == EventKind.OBSERVATION and e.step == StepName.APPROVE
    ]
    assert len(obs) == 1
    assert obs[0].state_before == LifecycleState.CANCELLED
    assert obs[0].state_after == LifecycleState.CANCELLED
    # The reshare_id is still recorded for audit.
    assert obs[0].payload["reshare_id"].startswith("rs-")


async def test_unknown_target_skipped_not_failed(sm: async_sessionmaker[AsyncSession]) -> None:
    row_id = await _enqueue(sm, target="nobody", payload={})

    worker = OutboxWorker(sm, handlers={})
    stats = await worker.drain_once()

    assert stats == DrainStats(skipped_no_handler=1)
    row = await _row(sm, row_id)
    assert row.status == "pending"
    assert row.attempts == 0  # we never even tried


async def test_reshare_handler_dispatches_to_client(sm: async_sessionmaker[AsyncSession]) -> None:
    """make_reshare_handler routes payload['action'] to the right client method."""
    client = MockReShareClient()
    handler: Handler = make_reshare_handler(client)

    # First enqueue: send_request creates a reshare row.
    send_idem = "outbox-send-1"
    await _enqueue(
        sm,
        target="reshare",
        payload={
            "action": "send_request",
            "args": {
                "request_payload": {"title": "X"},
                "supplier_symbol": "MEMBER1",
            },
        },
        idempotency_key=send_idem,
    )

    worker = OutboxWorker(sm, {"reshare": handler})
    stats = await worker.drain_once()
    assert stats == DrainStats(delivered=1)

    # Replaying the same idem key on the mock returns the prior result.
    prior = await client.send_request(
        idempotency_key=send_idem,
        request_payload={"title": "X"},
        supplier_symbol="MEMBER1",
    )
    assert prior.reshare_id.startswith("rs-")

    # Now enqueue a cancel against the synthesized reshare_id.
    await _enqueue(
        sm,
        target="reshare",
        payload={
            "action": "cancel_request",
            "args": {"reshare_id": prior.reshare_id, "reason": "test"},
        },
        idempotency_key="outbox-cancel-1",
    )
    stats2 = await worker.drain_once()
    assert stats2 == DrainStats(delivered=1)

    # Verify the cancel landed by replaying its idem key.
    cancelled = await client.cancel_request(
        idempotency_key="outbox-cancel-1",
        reshare_id=prior.reshare_id,
        reason="test",
    )
    assert cancelled.state == "Cancelled"


async def test_reshare_handler_rejects_unknown_action(sm: async_sessionmaker[AsyncSession]) -> None:
    client = MockReShareClient()
    handler = make_reshare_handler(client)

    row_id = await _enqueue(
        sm,
        target="reshare",
        payload={"action": "not_a_method", "args": {}},
    )

    worker = OutboxWorker(sm, {"reshare": handler}, base_backoff_secs=0)
    stats = await worker.drain_once()

    assert stats.failed == 1
    row = await _row(sm, row_id)
    assert "not_a_method" in (row.last_error or "")


async def test_ncip_handler_dispatches_check_out_and_check_in(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """make_ncip_handler routes payload['action'] to the right NCIP method.

    Mirror of test_reshare_handler_dispatches_to_client. Verifies that
    a flow writing target='ncip' rows lands check_out / check_in calls
    on the NCIP client with the row's idempotency key.
    """
    client = MockNcipClient()
    handler: Handler = make_ncip_handler(client)

    co_idem = "outbox-ncip-co-1"
    await _enqueue(
        sm,
        target="ncip",
        payload={
            "action": "check_out",
            "args": {"item_id": "item-42", "patron_id": "p1"},
        },
        idempotency_key=co_idem,
    )

    worker = OutboxWorker(sm, {"ncip": handler})
    stats = await worker.drain_once()
    assert stats == DrainStats(delivered=1)

    # Replay confirms the mock recorded the call under that idem key.
    prior = await client.check_out(
        idempotency_key=co_idem, item_id="item-42", patron_id="p1"
    )
    assert prior.state == "checked_out"
    assert prior.item_id == "item-42"

    # Round-trip with check_in.
    await _enqueue(
        sm,
        target="ncip",
        payload={"action": "check_in", "args": {"item_id": "item-42"}},
        idempotency_key="outbox-ncip-ci-1",
    )
    stats2 = await worker.drain_once()
    assert stats2 == DrainStats(delivered=1)

    in_replay = await client.check_in(
        idempotency_key="outbox-ncip-ci-1", item_id="item-42"
    )
    assert in_replay.state == "checked_in"


async def test_ncip_handler_rejects_unknown_action(sm: async_sessionmaker[AsyncSession]) -> None:
    """An unknown NCIP action surfaces as a failed outbox row, not a crash."""
    client = MockNcipClient()
    handler = make_ncip_handler(client)

    row_id = await _enqueue(
        sm,
        target="ncip",
        payload={"action": "not_a_method", "args": {}},
    )

    worker = OutboxWorker(sm, {"ncip": handler}, base_backoff_secs=0)
    stats = await worker.drain_once()

    assert stats.failed == 1
    row = await _row(sm, row_id)
    assert "not_a_method" in (row.last_error or "")


async def test_ncip_handler_rejects_malformed_payload(sm: async_sessionmaker[AsyncSession]) -> None:
    """Missing 'action' or non-dict 'args' must fail loudly, not silently."""
    client = MockNcipClient()
    handler = make_ncip_handler(client)

    # Missing action.
    row_id_a = await _enqueue(
        sm,
        target="ncip",
        payload={"args": {"item_id": "x"}},
        idempotency_key="ncip-bad-1",
    )
    # Non-dict args.
    row_id_b = await _enqueue(
        sm,
        target="ncip",
        payload={"action": "check_in", "args": "oops"},
        idempotency_key="ncip-bad-2",
    )

    worker = OutboxWorker(sm, {"ncip": handler}, base_backoff_secs=0)
    stats = await worker.drain_once()

    assert stats.failed == 2
    row_a = await _row(sm, row_id_a)
    row_b = await _row(sm, row_id_b)
    assert "missing 'action'" in (row_a.last_error or "")
    assert "must be dict" in (row_b.last_error or "")
