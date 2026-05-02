"""Saga coordinator end-to-end behaviour."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agora.agents.transaction import TransactionAgent
from agora.clients.ncip import MockNcipClient, NcipClient
from agora.clients.reshare import MockReShareClient, ReShareClient
from agora.models.events import NewSagaEvent, SagaEvent
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
from agora.saga.db import OutboxRow
from agora.saga.flows import build_registry
from agora.saga.idempotency import new_idempotency_key
from agora.saga.ledger import SagaLedger
from agora.saga.outbox import (
    OutboxWorker,
    make_ncip_handler,
    make_reshare_handler,
    make_reshare_on_success,
)
from agora.saga.steps import StepRegistry


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
async def test_forward_step_blocked_without_committed_gate(session: AsyncSession) -> None:
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))

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
async def test_happy_path_full_lifecycle(session: AsyncSession) -> None:
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    ncip = MockNcipClient()
    registry = build_registry(TransactionAgent(reshare))

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

    extras: dict[str, Any] = {"chosen_supplier": "B"}

    # ROUTE
    await _gate_and_run(session, registry, saga_id, request, StepName.ROUTE, extras)

    # APPROVE — forward enqueues send_request and lands in APPROVING.
    # The outbox worker calls the supplier, projects the ack as an
    # OBSERVATION event carrying ``reshare_id``, and advances the
    # saga to APPROVED. Per ADR-0012.
    approve_forward = await _gate_and_run(
        session, registry, saga_id, request, StepName.APPROVE, extras
    )
    assert approve_forward.state_after == LifecycleState.APPROVING
    assert "reshare_id" not in approve_forward.payload

    await _drain_with_projection(session, reshare, ncip)

    # Pull reshare_id off the projected OBSERVATION; downstream SHIP
    # / RETURN need it. Mirrors what ``api._derive_extras`` would do.
    async with session.begin():
        events = await SagaLedger(session).events_for(saga_id)
    approve_obs = next(
        e for e in events
        if e.kind == EventKind.OBSERVATION and e.step == StepName.APPROVE
    )
    extras["reshare_id"] = approve_obs.payload["reshare_id"]
    item_id = extras["reshare_id"]  # NCIP item_id approximation per flows.py

    # SHIP — forward fans out to two intents (reshare confirm_shipment +
    # ncip check_out). Drain so both land on their respective clients.
    await _gate_and_run(session, registry, saga_id, request, StepName.SHIP, extras)
    await _drain_with_projection(session, reshare, ncip)

    # RETURN — same fan-out (confirm_return + check_in).
    await _gate_and_run(
        session, registry, saga_id, request, StepName.RETURN_ITEM, extras
    )
    await _drain_with_projection(session, reshare, ncip)

    async with session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
    assert saga.current_state == LifecycleState.RETURNED.value

    # All saga outbox rows ended up delivered, no stuck pending rows.
    async with session.begin():
        outbox_rows = (
            (
                await session.execute(
                    select(OutboxRow)
                    .where(OutboxRow.saga_id == saga_id)
                    .order_by(OutboxRow.id)
                )
            )
            .scalars()
            .all()
        )
    assert all(r.status == "delivered" for r in outbox_rows), (
        "every saga outbox row must be delivered after final drain; "
        f"statuses={[r.status for r in outbox_rows]}"
    )
    targets = sorted(r.target for r in outbox_rows)
    # APPROVE: 1 reshare. SHIP: 1 reshare + 1 ncip. RETURN: 1 reshare + 1 ncip.
    assert targets == ["ncip", "ncip", "reshare", "reshare", "reshare"], (
        f"expected 3 reshare + 2 ncip rows, got {targets}"
    )

    # NCIP fan-out: replay each ncip row's *actual* idempotency key
    # against the mock to confirm the worker's call landed. The mock
    # dedups on the key and returns the prior NcipResult — a hit
    # proves the original dispatch ran.
    ncip_rows = [r for r in outbox_rows if r.target == "ncip"]
    co_row = next(r for r in ncip_rows if r.payload["action"] == "check_out")
    ci_row = next(r for r in ncip_rows if r.payload["action"] == "check_in")
    assert co_row.payload["args"]["item_id"] == item_id
    assert ci_row.payload["args"]["item_id"] == item_id

    co_replay = await ncip.check_out(
        idempotency_key=co_row.idempotency_key,
        item_id=item_id,
        patron_id=request.patron.patron_id,
    )
    assert co_replay.state == "checked_out"
    ci_replay = await ncip.check_in(
        idempotency_key=ci_row.idempotency_key, item_id=item_id
    )
    assert ci_replay.state == "checked_in"


@pytest.mark.asyncio
async def test_compensator_on_approve_cancels_at_supplier(session: AsyncSession) -> None:
    """APPROVE forward → drain → projection → APPROVE compensator → CANCELLED.

    Walks the full ADR-0012 path: forward enqueues, worker projects the
    OBSERVATION advancing APPROVING -> APPROVED, then the compensator
    pulls ``reshare_id`` from extras (sourced from the OBSERVATION via
    the same path ``api._derive_extras`` would use) and enqueues
    ``cancel_request``.
    """
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.ROUTED,
        )

    extras: dict[str, Any] = {"chosen_supplier": "B"}
    forward = await _gate_and_run(
        session,
        registry,
        saga_id,
        request,
        StepName.APPROVE,
        extras,
        from_state=LifecycleState.ROUTED,
    )
    assert forward.state_after == LifecycleState.APPROVING
    assert "reshare_id" not in forward.payload

    # Worker drains, supplier responds, projection advances to APPROVED.
    await _drain_with_projection(session, reshare)

    async with session.begin():
        events = await SagaLedger(session).events_for(saga_id)
    approve_obs = next(
        e for e in events
        if e.kind == EventKind.OBSERVATION and e.step == StepName.APPROVE
    )
    assert approve_obs.state_after == LifecycleState.APPROVED
    assert approve_obs.payload["reshare_id"]

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=LifecycleState.APPROVED,
            idempotency_key=new_idempotency_key(prefix="comp"),
            actor="agent:reconciliation",
            extras={"reshare_id": approve_obs.payload["reshare_id"]},
        )
        await coord.run_compensator(ctx=ctx, step=StepName.APPROVE)

    async with session.begin():
        ledger = SagaLedger(session)
        saga = await ledger.get_saga(saga_id)
    assert saga.current_state == LifecycleState.CANCELLED.value


async def _drain_with_projection(
    session: AsyncSession,
    reshare: ReShareClient,
    ncip: NcipClient | None = None,
) -> None:
    """Drain the outbox once with the production projection wired in.

    Mirrors the lifespan wiring (``api.app._build_outbox_worker``) so a
    test driving APPROVE forward can let the worker land its
    OBSERVATION (advancing APPROVING -> APPROVED) before continuing.
    Also wires the NCIP handler (defaulting to ``MockNcipClient``) so
    SHIP / RETURN forwards — which fan out a second ``target='ncip'``
    intent for borrower-side circulation — drain cleanly. Uses the
    same engine as the test's ``session`` fixture; aiosqlite in-memory
    shares state across connections in this test setup.
    """
    sessionmaker = async_sessionmaker(bind=session.bind, expire_on_commit=False)
    ncip_client = ncip if ncip is not None else MockNcipClient()
    worker = OutboxWorker(
        sessionmaker,
        handlers={
            "reshare": make_reshare_handler(reshare),
            "ncip": make_ncip_handler(ncip_client),
        },
        on_success={"reshare": make_reshare_on_success()},
    )
    await worker.drain_until_empty()


async def _gate_and_run(
    session: AsyncSession,
    registry: StepRegistry,
    saga_id: UUID,
    request: IllRequest,
    step: StepName,
    extras: dict[str, Any],
    *,
    from_state: LifecycleState | None = None,
) -> SagaEvent:
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


@pytest.mark.asyncio
async def test_approve_forward_enqueues_send_request_outbox_row(
    session: AsyncSession,
) -> None:
    """APPROVE forward (ADR-0012) is pure: ledger event + one OutboxIntent.

    The forward payload must NOT carry ``reshare_id`` — the supplier
    hasn't been called yet. The outbox row carries the full
    ``request_payload`` and ``supplier_symbol`` the worker will hand
    to ``ReShareClient.send_request``.
    """
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.ROUTED,
        )

    forward = await _gate_and_run(
        session,
        registry,
        saga_id,
        request,
        StepName.APPROVE,
        {"chosen_supplier": "MEMBER1"},
        from_state=LifecycleState.ROUTED,
    )

    # Forward landed in APPROVING with no reshare_id (worker hasn't
    # run yet).
    assert forward.state_after == LifecycleState.APPROVING
    assert "reshare_id" not in forward.payload
    assert forward.payload["supplier_symbol"] == "MEMBER1"
    assert forward.iso_message_id is None

    async with session.begin():
        rows = (
            (
                await session.execute(
                    select(OutboxRow).where(OutboxRow.saga_id == saga_id)
                )
            ).scalars().all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.target == "reshare"
    assert row.status == "pending"
    assert row.payload["action"] == "send_request"
    assert row.payload["args"]["supplier_symbol"] == "MEMBER1"
    assert row.payload["args"]["request_payload"]["request_id"] == str(
        request.request_id
    )

    # Mock client has NOT been called yet — the worker drains separately.
    # ``_idem`` is the mock's idempotency-key dedup map (private attr,
    # accessed deliberately for this assertion only).
    assert reshare._idem == {}, "supplier wire call must not have fired"


@pytest.mark.asyncio
async def test_approve_compensator_blocks_when_reshare_id_unavailable(
    session: AsyncSession,
) -> None:
    """If staff compensates while APPROVING (ack pending), raise loudly.

    Without a ``reshare_id`` the compensator has nothing concrete to
    cancel at the supplier. Surfacing as ``ValueError`` lets the API
    return a 400 with a staff-actionable message rather than
    enqueuing a malformed cancel.
    """
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.ROUTED,
        )

    # Run the forward but do NOT drain — saga sits in APPROVING with
    # no projected OBSERVATION yet.
    await _gate_and_run(
        session,
        registry,
        saga_id,
        request,
        StepName.APPROVE,
        {"chosen_supplier": "MEMBER1"},
        from_state=LifecycleState.ROUTED,
    )

    # Compensator with empty extras must reject — there is no
    # reshare_id to cancel against.
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=LifecycleState.APPROVING,
            idempotency_key=new_idempotency_key(prefix="comp"),
            actor="agent:reconciliation",
        )
        with pytest.raises(ValueError, match="supplier ack pending"):
            await coord.run_compensator(ctx=ctx, step=StepName.APPROVE)


@pytest.mark.asyncio
async def test_approve_projection_is_replay_safe(session: AsyncSession) -> None:
    """Re-draining the same APPROVE outbox row must not double-project.

    The OBSERVATION's deterministic ``idempotency_key`` (
    ``approve-ack-{row_id}``) makes the second projection a no-op:
    ``SagaLedger.append`` returns the existing row instead of writing
    a duplicate, so the saga sees exactly one
    APPROVING -> APPROVED transition.
    """
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.ROUTED,
        )

    await _gate_and_run(
        session,
        registry,
        saga_id,
        request,
        StepName.APPROVE,
        {"chosen_supplier": "MEMBER1"},
        from_state=LifecycleState.ROUTED,
    )

    # First drain: writes the OBSERVATION + marks delivered.
    await _drain_with_projection(session, reshare)

    # Forge a fresh "pending" row pointing at the same saga so the
    # projection runs again with the same row_id. The simplest way
    # is to call drain a second time — already-delivered rows are
    # skipped, so we instead re-insert a synthetic pending row that
    # reuses the existing row_id... too invasive. Easier and more
    # honest: hand-invoke the on_success callback twice and assert
    # the second call returns without writing a second observation.
    sm = async_sessionmaker(bind=session.bind, expire_on_commit=False)
    async with session.begin():
        events_after_first = await SagaLedger(session).events_for(saga_id)
    obs_first = [
        e for e in events_after_first
        if e.kind == EventKind.OBSERVATION and e.step == StepName.APPROVE
    ]
    assert len(obs_first) == 1, "first drain must write exactly one OBSERVATION"
    row_id_used = obs_first[0].payload["source_outbox_row_id"]

    on_success = make_reshare_on_success()
    fake_result = await reshare.send_request(
        idempotency_key="dup-call",
        request_payload={"request_id": str(request.request_id)},
        supplier_symbol="MEMBER1",
    )
    async with sm() as replay_session:
        await on_success(
            replay_session,
            row_id_used,
            saga_id,
            {"action": "send_request"},
            "approve-replay-key",
            fake_result,
        )
        await replay_session.commit()

    async with session.begin():
        events_after_replay = await SagaLedger(session).events_for(saga_id)
    obs_after_replay = [
        e for e in events_after_replay
        if e.kind == EventKind.OBSERVATION and e.step == StepName.APPROVE
    ]
    assert len(obs_after_replay) == 1, (
        "projection replay with same row_id must not duplicate the OBSERVATION"
    )


@pytest.mark.asyncio
async def test_ship_forward_enqueues_outbox_row(session: AsyncSession) -> None:
    """SHIP forward returns an OutboxIntent; coordinator must enqueue it."""
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.APPROVED,
        )

    extras = {"reshare_id": "rs-test-1"}
    await _gate_and_run(
        session,
        registry,
        saga_id,
        request,
        StepName.SHIP,
        extras,
        from_state=LifecycleState.APPROVED,
    )

    # SHIP forward emits two intents: ReShare confirm_shipment for the
    # consortium peer, and NCIP check_out against the borrower's local
    # ILS. Both rows pending; the worker has not run, so neither client
    # has received the call.
    async with session.begin():
        rows = (
            (
                await session.execute(
                    select(OutboxRow)
                    .where(OutboxRow.saga_id == saga_id)
                    .order_by(OutboxRow.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    by_target = {r.target: r for r in rows}
    assert set(by_target) == {"reshare", "ncip"}
    assert all(r.status == "pending" for r in rows)
    assert by_target["reshare"].payload == {
        "action": "confirm_shipment",
        "args": {"reshare_id": "rs-test-1"},
    }
    assert by_target["ncip"].payload == {
        "action": "check_out",
        "args": {"item_id": "rs-test-1", "patron_id": request.patron.patron_id},
    }
    # idempotency keys must differ across targets (UNIQUE constraint).
    assert (
        by_target["reshare"].idempotency_key
        != by_target["ncip"].idempotency_key
    )
    assert by_target["ncip"].idempotency_key.endswith(":ncip")


@pytest.mark.asyncio
async def test_replayed_forward_does_not_double_enqueue(session: AsyncSession) -> None:
    """Re-running a forward with the same idempotency_key skips outbox enqueue."""
    saga_id = uuid4()
    request = _build_request()
    reshare = MockReShareClient()
    registry = build_registry(TransactionAgent(reshare))

    async with session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.APPROVED,
        )

    # First run: gate + forward with a known idempotency key.
    fixed_key = new_idempotency_key(prefix="ship-replay")

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        await coord.open_gate(saga_id=saga_id, step=StepName.SHIP, actor="staff:t")
        await coord.commit_gate(
            saga_id=saga_id, step=StepName.SHIP, actor="staff:t", rationale="ok"
        )

    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=LifecycleState.APPROVED,
            idempotency_key=fixed_key,
            actor="agent:transaction",
            extras={"reshare_id": "rs-replay-1"},
        )
        await coord.run_forward(ctx=ctx, step=StepName.SHIP)

    # Replay: same idempotency_key. Ledger.append returns None → coordinator
    # must skip enqueue. Result: still exactly one outbox row.
    async with session.begin():
        coord = Coordinator(session=session, registry=registry)
        ctx = SagaContext(
            saga_id=saga_id,
            request=request,
            current_state=LifecycleState.SHIPPED,
            idempotency_key=fixed_key,
            actor="agent:transaction",
            extras={"reshare_id": "rs-replay-1"},
        )
        await coord.run_forward(ctx=ctx, step=StepName.SHIP, require_gate=False)

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
    # SHIP emits two intents (reshare + ncip); replay must not double either.
    assert len(rows) == 2, "replayed forward must not enqueue duplicate outbox rows"
    assert {r.target for r in rows} == {"reshare", "ncip"}
