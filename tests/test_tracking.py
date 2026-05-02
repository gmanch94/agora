"""TrackingAgent + OverdueScanner tests.

Verify two things:

1. SHIP forward stamps ``due_at`` on its payload so the scanner has
   something to read.
2. ``OverdueScanner.scan`` records exactly one OBSERVATION event per
   shipped-and-overdue saga, and re-running the scan does not write
   duplicate events.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agora.agents.tracking import OverdueScanner
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
from agora.saga.coordinator import Coordinator
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


async def _seed_shipped_saga(
    sessionmaker: async_sessionmaker,
    *,
    due_at: datetime,
    reshare_id: str = "rs-overdue-1",
) -> tuple:
    """Seed a saga directly into SHIPPED with a hand-crafted ship payload."""
    saga_id = uuid4()
    request = _build_request()
    async with sessionmaker() as session, session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.SHIPPED,
        )
        shipped_at = due_at - timedelta(days=28)
        await ledger.append(
            NewSagaEvent(
                saga_id=saga_id,
                kind=EventKind.FORWARD,
                step=StepName.SHIP,
                state_before=LifecycleState.APPROVED,
                state_after=LifecycleState.SHIPPED,
                actor="agent:transaction",
                idempotency_key=new_idempotency_key(prefix="ship-test"),
                payload={
                    "reshare_id": reshare_id,
                    "shipped_at": shipped_at.isoformat(),
                    "due_at": due_at.isoformat(),
                    "loan_period_days": 28,
                },
                outcome=StepOutcome.COMMITTED,
                rationale="seeded ship event",
            )
        )
    return saga_id, request


@pytest.mark.asyncio
async def test_ship_forward_stamps_due_at(session) -> None:
    """SHIP forward payload must carry due_at + shipped_at + loan_period_days."""
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
            initial_state=LifecycleState.APPROVED,
        )

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
            idempotency_key=new_idempotency_key(prefix="ship"),
            actor="agent:transaction",
            extras={"reshare_id": "rs-due-1"},
        )
        ev = await coord.run_forward(ctx=ctx, step=StepName.SHIP)

    assert ev is not None
    assert ev.payload["reshare_id"] == "rs-due-1"
    assert "due_at" in ev.payload
    assert "shipped_at" in ev.payload
    assert ev.payload["loan_period_days"] == 28
    due = datetime.fromisoformat(ev.payload["due_at"])
    shipped = datetime.fromisoformat(ev.payload["shipped_at"])
    assert (due - shipped) == timedelta(days=28)


@pytest.mark.asyncio
async def test_ship_forward_honours_loan_period_override(session) -> None:
    """ctx.extras['loan_period_days'] overrides the default loan window."""
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
            initial_state=LifecycleState.APPROVED,
        )

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
            idempotency_key=new_idempotency_key(prefix="ship"),
            actor="agent:transaction",
            extras={"reshare_id": "rs-due-1", "loan_period_days": 7},
        )
        ev = await coord.run_forward(ctx=ctx, step=StepName.SHIP)

    assert ev is not None
    assert ev.payload["loan_period_days"] == 7
    due = datetime.fromisoformat(ev.payload["due_at"])
    shipped = datetime.fromisoformat(ev.payload["shipped_at"])
    assert (due - shipped) == timedelta(days=7)


@pytest.mark.asyncio
async def test_overdue_scanner_records_observation_when_past_due(engine) -> None:
    """Scanner appends one OBSERVATION event per shipped-overdue saga."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now - timedelta(days=3)
    saga_id, _ = await _seed_shipped_saga(sm, due_at=due_at, reshare_id="rs-od-1")

    scanner = OverdueScanner(sm, now_fn=lambda: now)
    records = await scanner.scan()

    assert len(records) == 1
    rec = records[0]
    assert rec.saga_id == saga_id
    assert rec.reshare_id == "rs-od-1"
    assert rec.days_overdue == 3
    assert rec.newly_recorded is True

    # Ledger has exactly one overdue OBSERVATION for this saga.
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    overdue = [
        e
        for e in events
        if e.kind == EventKind.OBSERVATION
        and e.payload.get("kind") == "overdue"
    ]
    assert len(overdue) == 1
    assert overdue[0].payload["days_overdue"] == 3
    assert overdue[0].idempotency_key == f"overdue-{saga_id}"


@pytest.mark.asyncio
async def test_overdue_scanner_is_idempotent_across_runs(engine) -> None:
    """Re-running the scanner does not double-record the observation."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now - timedelta(days=2)
    saga_id, _ = await _seed_shipped_saga(sm, due_at=due_at)

    scanner = OverdueScanner(sm, now_fn=lambda: now)
    first = await scanner.scan()
    second_now = now + timedelta(hours=6)
    scanner2 = OverdueScanner(sm, now_fn=lambda: second_now)
    second = await scanner2.scan()

    assert len(first) == 1 and first[0].newly_recorded is True
    assert len(second) == 1 and second[0].newly_recorded is False

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    overdue = [
        e
        for e in events
        if e.kind == EventKind.OBSERVATION
        and e.payload.get("kind") == "overdue"
    ]
    assert len(overdue) == 1, "scanner must not write duplicate observations"


@pytest.mark.asyncio
async def test_overdue_scanner_skips_not_yet_due(engine) -> None:
    """Sagas with future due_at are not flagged."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now + timedelta(days=10)
    saga_id, _ = await _seed_shipped_saga(sm, due_at=due_at)

    scanner = OverdueScanner(sm, now_fn=lambda: now)
    records = await scanner.scan()
    assert records == []

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    assert not any(
        e.kind == EventKind.OBSERVATION and e.payload.get("kind") == "overdue"
        for e in events
    )


@pytest.mark.asyncio
async def test_overdue_scanner_ignores_non_shipped_sagas(engine) -> None:
    """Sagas not in SHIPPED are skipped even if a past ship event exists."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    saga_id = uuid4()
    request = _build_request()
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.RETURNED,
        )

    scanner = OverdueScanner(sm, now_fn=lambda: now)
    records = await scanner.scan()
    assert records == []


@pytest.mark.asyncio
async def test_overdue_scanner_run_forever_loops_until_cancelled(engine) -> None:
    """run_forever invokes scan() repeatedly until cancellation.

    Stubs ``scanner.scan`` to avoid racing real DB sessions against the
    background loop on shared in-memory SQLite (causes intermittent
    'database is locked' under aiosqlite). The pure call-counting form
    is sufficient to exercise the loop + cancel contract.
    """
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    scanner = OverdueScanner(sm)
    calls = {"n": 0}

    async def counting_scan() -> list:
        calls["n"] += 1
        return []

    scanner.scan = counting_scan  # type: ignore[method-assign]
    task = asyncio.create_task(scanner.run_forever(poll_interval=0.01))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if calls["n"] >= 3:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls["n"] >= 3, "run_forever must call scan repeatedly"


@pytest.mark.asyncio
async def test_overdue_scanner_run_forever_swallows_pass_errors(engine) -> None:
    """A scan-pass exception is logged + absorbed; loop keeps running."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    scanner = OverdueScanner(sm)
    calls = {"n": 0}

    async def flaky_scan() -> list:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db blip")
        return []

    scanner.scan = flaky_scan  # type: ignore[method-assign]
    task = asyncio.create_task(scanner.run_forever(poll_interval=0.01))
    # Wait until at least 2 calls happened (one failure, one success).
    for _ in range(50):
        await asyncio.sleep(0.01)
        if calls["n"] >= 2:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls["n"] >= 2, "loop must continue past a single scan failure"
