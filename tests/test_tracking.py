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
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agora.agents.tracking import OverdueRecord, OverdueScanner
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
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    due_at: datetime,
    reshare_id: str = "rs-overdue-1",
) -> tuple[UUID, IllRequest]:
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
async def test_ship_forward_stamps_due_at(session: AsyncSession) -> None:
    """SHIP forward payload must carry due_at + shipped_at + loan_period_days."""
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
async def test_ship_forward_honours_loan_period_override(session: AsyncSession) -> None:
    """ctx.extras['loan_period_days'] overrides the default loan window."""
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
async def test_overdue_scanner_records_observation_when_past_due(engine: AsyncEngine) -> None:
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
async def test_overdue_scanner_is_idempotent_across_runs(engine: AsyncEngine) -> None:
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
async def test_overdue_scanner_skips_not_yet_due(engine: AsyncEngine) -> None:
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
async def test_overdue_scanner_ignores_non_shipped_sagas(engine: AsyncEngine) -> None:
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
async def test_overdue_scanner_emits_recall_proposed_past_threshold(
    engine: AsyncEngine,
) -> None:
    """Past ``recall_after_days``, scanner appends a recall_proposed obs."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now - timedelta(days=15)  # 15 >= 14 default threshold
    saga_id, _ = await _seed_shipped_saga(sm, due_at=due_at, reshare_id="rs-r-1")

    scanner = OverdueScanner(sm, now_fn=lambda: now, recall_after_days=14)
    records = await scanner.scan()

    assert len(records) == 1
    rec = records[0]
    assert rec.days_overdue == 15
    assert rec.recall_proposed is True
    assert rec.recall_proposed_newly is True
    # Tier-1 still emitted alongside tier-2.
    assert rec.newly_recorded is True

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    obs_kinds = sorted(
        str(e.payload.get("kind", ""))
        for e in events
        if e.kind == EventKind.OBSERVATION
    )
    assert obs_kinds == ["overdue", "recall_proposed"]
    recall_obs = next(
        e
        for e in events
        if e.kind == EventKind.OBSERVATION
        and e.payload.get("kind") == "recall_proposed"
    )
    assert recall_obs.idempotency_key == f"recall-proposed-{saga_id}"
    assert recall_obs.payload["suggested_action"] == "compensate_ship"
    assert recall_obs.payload["reshare_id"] == "rs-r-1"
    assert recall_obs.payload["threshold_days"] == 14


@pytest.mark.asyncio
async def test_overdue_scanner_no_recall_below_threshold(
    engine: AsyncEngine,
) -> None:
    """Below ``recall_after_days``, only tier-1 overdue is written."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now - timedelta(days=3)  # 3 < 14
    saga_id, _ = await _seed_shipped_saga(sm, due_at=due_at)

    scanner = OverdueScanner(sm, now_fn=lambda: now, recall_after_days=14)
    records = await scanner.scan()

    assert len(records) == 1
    assert records[0].recall_proposed is False
    assert records[0].recall_proposed_newly is False

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    assert not any(
        e.kind == EventKind.OBSERVATION
        and e.payload.get("kind") == "recall_proposed"
        for e in events
    )


@pytest.mark.asyncio
async def test_overdue_scanner_recall_is_idempotent_across_runs(
    engine: AsyncEngine,
) -> None:
    """Re-running past threshold must not write a duplicate recall obs."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now - timedelta(days=20)
    saga_id, _ = await _seed_shipped_saga(sm, due_at=due_at)

    scanner = OverdueScanner(sm, now_fn=lambda: now, recall_after_days=14)
    first = await scanner.scan()

    later = now + timedelta(days=2)
    scanner2 = OverdueScanner(sm, now_fn=lambda: later, recall_after_days=14)
    second = await scanner2.scan()

    assert first[0].recall_proposed_newly is True
    assert second[0].recall_proposed is True
    assert second[0].recall_proposed_newly is False

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    recall_obs = [
        e
        for e in events
        if e.kind == EventKind.OBSERVATION
        and e.payload.get("kind") == "recall_proposed"
    ]
    assert len(recall_obs) == 1, "scanner must not double-emit recall_proposed"


@pytest.mark.asyncio
async def test_overdue_scanner_recall_writes_no_outbox(
    engine: AsyncEngine,
) -> None:
    """Recall escalation is advisory: zero outbox rows after a tier-2 scan.

    Hard invariant from ADR-0005: agents recommend, staff commits. The
    scanner must never enqueue a recall on the wire — it surfaces a
    suggestion and waits for ``POST /sagas/{id}/compensate``.
    """
    from sqlalchemy import select as _select

    from agora.saga.db import OutboxRow

    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now - timedelta(days=30)
    await _seed_shipped_saga(sm, due_at=due_at)

    scanner = OverdueScanner(sm, now_fn=lambda: now, recall_after_days=14)
    await scanner.scan()

    async with sm() as session, session.begin():
        rows = (await session.execute(_select(OutboxRow))).scalars().all()
    assert rows == [], "scanner must never enqueue outbox intents"


@pytest.mark.asyncio
async def test_overdue_scanner_run_forever_loops_until_cancelled(
    engine: AsyncEngine,
) -> None:
    """run_forever invokes scan() repeatedly until cancellation.

    Stubs ``scanner.scan`` to avoid racing real DB sessions against the
    background loop on shared in-memory SQLite (causes intermittent
    'database is locked' under aiosqlite). The pure call-counting form
    is sufficient to exercise the loop + cancel contract.
    """
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    scanner = OverdueScanner(sm)
    calls = {"n": 0}

    async def counting_scan() -> list[OverdueRecord]:
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
async def test_overdue_scanner_run_forever_swallows_pass_errors(
    engine: AsyncEngine,
) -> None:
    """A scan-pass exception is logged + absorbed; loop keeps running."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    scanner = OverdueScanner(sm)
    calls = {"n": 0}

    async def flaky_scan() -> list[OverdueRecord]:
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
