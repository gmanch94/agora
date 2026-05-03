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
    shipped_at: datetime | None = None,
    loan_period_days: int = 28,
) -> tuple[UUID, IllRequest]:
    """Seed a saga directly into SHIPPED with a hand-crafted ship payload.

    ``shipped_at`` defaults to ``due_at - loan_period_days`` to mirror
    real SHIP forward output. Tier-3 (transit-time-since-shipped)
    tests can pass an explicit ``shipped_at`` decoupled from
    ``due_at`` to exercise scenarios like "shipped recently but with
    an unusual due_at" (e.g. backdated loan).
    """
    saga_id = uuid4()
    request = _build_request()
    if shipped_at is None:
        shipped_at = due_at - timedelta(days=loan_period_days)
    async with sessionmaker() as session, session.begin():
        ledger = SagaLedger(session)
        await ledger.create_saga(
            saga_id=saga_id,
            request_id=request.request_id,
            request_payload=request.model_dump(mode="json"),
            initial_state=LifecycleState.SHIPPED,
        )
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
                    "loan_period_days": loan_period_days,
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
    """Sagas with future due_at are not flagged.

    Pins ``unconfirmed_receipt_after_days`` high to keep this test
    focused on tier-1 (overdue). The seed helper anchors
    ``shipped_at`` 28 days before ``due_at``, which would otherwise
    trip the tier-3 transit-time watch on this saga.
    """
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now + timedelta(days=10)
    saga_id, _ = await _seed_shipped_saga(sm, due_at=due_at)

    scanner = OverdueScanner(
        sm, now_fn=lambda: now, unconfirmed_receipt_after_days=999
    )
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

    # Pin ``unconfirmed_receipt_after_days`` high so this test stays
    # focused on tier-1+2; the seed helper's 28-day shipped_at offset
    # would otherwise trip tier-3.
    scanner = OverdueScanner(
        sm,
        now_fn=lambda: now,
        recall_after_days=14,
        unconfirmed_receipt_after_days=999,
    )
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
async def test_tier3_emits_receipt_unconfirmed_past_threshold(
    engine: AsyncEngine,
) -> None:
    """Tier-3: shipped >= threshold days ago + saga still SHIPPED → emit.

    Post NCIP-checkout SHIP→RECEIVE re-anchor (PR #38), a saga stuck
    in SHIPPED has no NCIP ``check_out`` dispatched yet (the patron's
    ILS shows nothing). The scanner emits a deterministic
    ``receipt-unconfirmed-{saga_id}`` advisory to nudge staff.
    Independent of tier-1/2; fires on transit time, not loan-clock.
    """
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    # shipped 8 days ago, due_at 20 days from now → tier-3 only
    # (due_at in future, recall threshold inactive).
    shipped_at = now - timedelta(days=8)
    due_at = now + timedelta(days=20)
    saga_id, _ = await _seed_shipped_saga(
        sm, due_at=due_at, shipped_at=shipped_at, reshare_id="rs-tier3-1"
    )

    scanner = OverdueScanner(
        sm,
        now_fn=lambda: now,
        unconfirmed_receipt_after_days=7,
    )
    records = await scanner.scan()

    assert len(records) == 1
    rec = records[0]
    assert rec.saga_id == saga_id
    assert rec.reshare_id == "rs-tier3-1"
    assert rec.receipt_unconfirmed is True
    assert rec.receipt_unconfirmed_newly is True
    assert rec.days_since_shipped == 8
    # Tier-1/2 didn't fire — due_at is in the future.
    assert rec.newly_recorded is False
    assert rec.recall_proposed is False
    assert rec.due_at == due_at  # carried through for context
    assert rec.shipped_at == shipped_at

    # Ledger has the deterministic key + payload shape.
    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    rcu_obs = [
        e
        for e in events
        if e.kind == EventKind.OBSERVATION
        and e.payload.get("kind") == "receipt_unconfirmed"
    ]
    assert len(rcu_obs) == 1
    obs = rcu_obs[0]
    assert obs.idempotency_key == f"receipt-unconfirmed-{saga_id}"
    assert obs.payload["reshare_id"] == "rs-tier3-1"
    assert obs.payload["days_since_shipped"] == 8
    assert obs.payload["threshold_days"] == 7
    # No suggested_action — scope-discipline (no staff-console hook
    # for it yet; advisor's recommendation per PR description).
    assert "suggested_action" not in obs.payload


@pytest.mark.asyncio
async def test_tier3_skips_below_threshold(engine: AsyncEngine) -> None:
    """Saga shipped less than threshold days ago → no tier-3 emission."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    # shipped 3 days ago, well under default 7-day threshold
    shipped_at = now - timedelta(days=3)
    due_at = now + timedelta(days=25)
    saga_id, _ = await _seed_shipped_saga(
        sm, due_at=due_at, shipped_at=shipped_at
    )

    scanner = OverdueScanner(
        sm, now_fn=lambda: now, unconfirmed_receipt_after_days=7
    )
    records = await scanner.scan()
    assert records == []

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    assert not any(
        e.kind == EventKind.OBSERVATION
        and e.payload.get("kind") == "receipt_unconfirmed"
        for e in events
    )


@pytest.mark.asyncio
async def test_tier3_idempotent_across_runs(engine: AsyncEngine) -> None:
    """Re-running the scanner does not double-emit receipt_unconfirmed."""
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    shipped_at = now - timedelta(days=10)
    due_at = now + timedelta(days=18)
    saga_id, _ = await _seed_shipped_saga(
        sm, due_at=due_at, shipped_at=shipped_at
    )

    scanner = OverdueScanner(
        sm, now_fn=lambda: now, unconfirmed_receipt_after_days=7
    )
    first = await scanner.scan()
    later = now + timedelta(hours=6)
    scanner2 = OverdueScanner(
        sm, now_fn=lambda: later, unconfirmed_receipt_after_days=7
    )
    second = await scanner2.scan()

    assert first[0].receipt_unconfirmed_newly is True
    assert second[0].receipt_unconfirmed is True
    assert second[0].receipt_unconfirmed_newly is False

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    rcu_obs = [
        e
        for e in events
        if e.kind == EventKind.OBSERVATION
        and e.payload.get("kind") == "receipt_unconfirmed"
    ]
    assert len(rcu_obs) == 1


@pytest.mark.asyncio
async def test_tier3_only_fires_while_at_shipped(engine: AsyncEngine) -> None:
    """A saga that has progressed past SHIPPED is excluded by SQL filter.

    The scanner's ``current_state == SHIPPED`` filter is the
    authoritative "patron hasn't confirmed receipt" signal — once the
    patron clicks RECEIVE the saga moves to RECEIVED and naturally
    drops out of future scans.
    """
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    shipped_at = now - timedelta(days=15)
    due_at = now + timedelta(days=13)
    saga_id, _ = await _seed_shipped_saga(
        sm, due_at=due_at, shipped_at=shipped_at
    )

    # Manually advance the saga past SHIPPED (mimics the projection
    # that RECEIVE forward would write). We bypass the coordinator
    # here because the test only cares about the scanner's filter.
    async with sm() as session, session.begin():
        from agora.saga.db import Saga as SagaORM
        saga_row = (await session.get(SagaORM, saga_id))
        assert saga_row is not None
        saga_row.current_state = LifecycleState.RECEIVED.value

    scanner = OverdueScanner(
        sm, now_fn=lambda: now, unconfirmed_receipt_after_days=7
    )
    records = await scanner.scan()
    assert records == []


@pytest.mark.asyncio
async def test_tier3_concurrent_with_tier1_and_tier2(engine: AsyncEngine) -> None:
    """All three tiers can fire on the same saga independently.

    A saga shipped 30 days ago with a 28-day loan period is overdue
    by 2 days (tier-1) and well past the 7-day transit threshold
    (tier-3). Tier-2 is gated to fire only past ``recall_after_days``;
    we pin it to 1 here so all three light up.
    """
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    due_at = now - timedelta(days=2)  # 2 days overdue
    saga_id, _ = await _seed_shipped_saga(sm, due_at=due_at)
    # Helper defaults shipped_at = due_at - 28 days = -30 from now.

    scanner = OverdueScanner(
        sm,
        now_fn=lambda: now,
        recall_after_days=1,  # 2 >= 1 → tier-2 fires
        unconfirmed_receipt_after_days=7,  # 30 >= 7 → tier-3 fires
    )
    records = await scanner.scan()

    assert len(records) == 1
    rec = records[0]
    assert rec.newly_recorded is True
    assert rec.recall_proposed_newly is True
    assert rec.receipt_unconfirmed_newly is True
    assert rec.days_overdue == 2
    assert rec.days_since_shipped == 30

    async with sm() as session, session.begin():
        ledger = SagaLedger(session)
        events = await ledger.events_for(saga_id)
    obs_kinds = sorted(
        str(e.payload.get("kind", ""))
        for e in events
        if e.kind == EventKind.OBSERVATION
    )
    assert obs_kinds == ["overdue", "recall_proposed", "receipt_unconfirmed"]


@pytest.mark.asyncio
async def test_tier3_writes_no_outbox(engine: AsyncEngine) -> None:
    """Tier-3 is advisory: no outbox row enqueued (ADR-0005).

    Same hard invariant as tier-2 (and indeed every scanner-emitted
    observation): agents recommend, staff commits.
    """
    from sqlalchemy import select as _select

    from agora.saga.db import OutboxRow

    sm = async_sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime(2026, 6, 1, tzinfo=UTC)
    shipped_at = now - timedelta(days=10)
    due_at = now + timedelta(days=18)
    await _seed_shipped_saga(sm, due_at=due_at, shipped_at=shipped_at)

    scanner = OverdueScanner(
        sm, now_fn=lambda: now, unconfirmed_receipt_after_days=7
    )
    await scanner.scan()

    async with sm() as session, session.begin():
        rows = (await session.execute(_select(OutboxRow))).scalars().all()
    assert rows == [], "tier-3 must never enqueue outbox intents"


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
