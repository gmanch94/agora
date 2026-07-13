"""PatronScrubber + RetentionScanner tests (G-07, ADR-0020).

Exercises:

- Fingerprint determinism (same patron_id + salt -> same fingerprint).
- Scrub mutates ``request_payload`` in place and writes an
  OBSERVATION event with the deterministic idempotency key.
- Re-running the scrub on an already-scrubbed saga is a no-op.
- ``RetentionScanner.scan`` only touches sagas in
  ``SCRUB_ELIGIBLE_STATES`` past the retention window; DISPUTED
  sagas are excluded.
- ``fingerprint_patron`` raises ``RetentionConfigError`` on empty salt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agora.agents.retention import (
    SCRUB_ELIGIBLE_STATES,
    SCRUBBED_PREFIX,
    PatronScrubber,
    RetentionConfigError,
    RetentionScanner,
    fingerprint_patron,
    is_scrubbed,
)
from agora.models.lifecycle import LifecycleState
from agora.saga.db import Saga, SagaEventRow

# 64-char hex meets MIN_SCRUB_SALT_LEN (32). Deterministic across runs.
SALT = "0" * 32 + "abcdef" * 5 + "ab"


# ---------------------------------------------------------------------
# Pure-fn unit tests
# ---------------------------------------------------------------------


def test_fingerprint_is_deterministic() -> None:
    a = fingerprint_patron("patron-001", SALT)
    b = fingerprint_patron("patron-001", SALT)
    assert a == b
    assert a.startswith(SCRUBBED_PREFIX)


def test_fingerprint_differs_per_patron() -> None:
    a = fingerprint_patron("patron-001", SALT)
    b = fingerprint_patron("patron-002", SALT)
    assert a != b


def test_fingerprint_differs_per_salt() -> None:
    salt_a = "A" * 32 + "1" * 32
    salt_b = "B" * 32 + "1" * 32
    a = fingerprint_patron("patron-001", salt_a)
    b = fingerprint_patron("patron-001", salt_b)
    assert a != b


def test_fingerprint_rejects_empty_salt() -> None:
    with pytest.raises(RetentionConfigError, match="at least 32 chars"):
        fingerprint_patron("patron-001", "")


def test_fingerprint_rejects_short_salt() -> None:
    """Reviewer MEDIUM: 1-byte salt is effectively zero entropy."""
    with pytest.raises(RetentionConfigError, match="at least 32 chars"):
        fingerprint_patron("patron-001", "x" * 16)


def test_fingerprint_rejects_whitespace_only_salt() -> None:
    with pytest.raises(RetentionConfigError, match="at least 32 chars"):
        fingerprint_patron("patron-001", "   ")


def test_is_scrubbed_detects_prefix() -> None:
    assert is_scrubbed(SCRUBBED_PREFIX + "abc")
    assert not is_scrubbed("patron-001")
    assert not is_scrubbed(None)


def test_scrub_eligible_states_excludes_disputed() -> None:
    """DISPUTED is terminal but NOT scrub-eligible — open issue."""
    assert LifecycleState.RETURNED in SCRUB_ELIGIBLE_STATES
    assert LifecycleState.CANCELLED in SCRUB_ELIGIBLE_STATES
    assert LifecycleState.UNFILLED in SCRUB_ELIGIBLE_STATES
    assert LifecycleState.DISPUTED not in SCRUB_ELIGIBLE_STATES


# ---------------------------------------------------------------------
# DB-touching tests
# ---------------------------------------------------------------------


async def _seed_saga(
    sm: async_sessionmaker[AsyncSession],
    *,
    state: LifecycleState,
    patron_id: str = "patron-001",
    updated_at: datetime | None = None,
    item_barcode: str | None = "BC-0001",
) -> UUID:
    """Seed a saga directly via ORM so tests don't have to drive the saga."""
    saga_id = uuid4()
    async with sm() as session, session.begin():
        payload = {
            "request_type": "loan",
            "patron": {"library_symbol": "A", "patron_id": patron_id},
            "requesting_library": {"symbol": "A", "name": "Library A"},
            "item": {
                "title": "Brave New World",
                "author": "Huxley",
                "isbn": "9780060850524",
                "item_barcode": item_barcode,
            },
        }
        saga = Saga(
            id=saga_id,
            request_id=uuid4(),
            current_state=state.value,
            request_payload=payload,
        )
        if updated_at is not None:
            saga.updated_at = updated_at
        session.add(saga)
    return saga_id


@pytest.fixture
def sessionmaker_(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Concrete sessionmaker bound to the in-memory engine."""
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def test_scrub_mutates_payload_and_writes_observation(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    saga_id = await _seed_saga(sessionmaker_, state=LifecycleState.RETURNED)
    scrubber = PatronScrubber(salt=SALT)

    async with sessionmaker_() as session, session.begin():
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        result = await scrubber.scrub(session, saga)

    assert result.scrubbed is True
    assert result.fingerprint is not None

    # Re-read saga + verify scrub
    async with sessionmaker_() as session:
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        pid = saga.request_payload["patron"]["patron_id"]
        assert isinstance(pid, str)
        assert pid.startswith(SCRUBBED_PREFIX)
        # item_barcode must be nulled
        assert saga.request_payload["item"]["item_barcode"] is None

        # OBSERVATION event with the deterministic key exists
        events = (
            (
                await session.execute(
                    select(SagaEventRow).where(
                        SagaEventRow.idempotency_key == f"patron_scrubbed-{saga_id}"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].actor == "agent:retention"


async def test_scrub_is_idempotent(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Replay must be a no-op: second scrub returns ScrubResult(scrubbed=False)."""
    saga_id = await _seed_saga(sessionmaker_, state=LifecycleState.RETURNED)
    scrubber = PatronScrubber(salt=SALT)

    async with sessionmaker_() as session, session.begin():
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        await scrubber.scrub(session, saga)

    async with sessionmaker_() as session, session.begin():
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        second = await scrubber.scrub(session, saga)
        assert second.scrubbed is False
        assert second.fingerprint is None


async def test_scanner_skips_non_terminal_states(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Active sagas (SHIPPED, ROUTED, etc.) must NEVER be scrubbed."""
    long_ago = datetime.now(UTC) - timedelta(days=365)
    saga_id = await _seed_saga(
        sessionmaker_, state=LifecycleState.SHIPPED, updated_at=long_ago
    )
    scrubber = PatronScrubber(salt=SALT)
    scanner = RetentionScanner(
        sessionmaker=sessionmaker_,
        scrubber=scrubber,
        retention_days=90,
        interval_secs=3600.0,
    )
    results = await scanner.scan()
    assert results == []  # SHIPPED is excluded by the eligibility filter

    async with sessionmaker_() as session:
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        assert not is_scrubbed(saga.request_payload["patron"]["patron_id"])


async def test_scanner_skips_disputed(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """DISPUTED is terminal but excluded — open issue still needs evidence."""
    long_ago = datetime.now(UTC) - timedelta(days=365)
    saga_id = await _seed_saga(
        sessionmaker_, state=LifecycleState.DISPUTED, updated_at=long_ago
    )
    scrubber = PatronScrubber(salt=SALT)
    scanner = RetentionScanner(
        sessionmaker=sessionmaker_,
        scrubber=scrubber,
        retention_days=90,
        interval_secs=3600.0,
    )
    results = await scanner.scan()
    assert results == []

    async with sessionmaker_() as session:
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        assert not is_scrubbed(saga.request_payload["patron"]["patron_id"])


async def test_scanner_skips_recent_terminal_sagas(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal sagas within the retention window stay intact."""
    recent = datetime.now(UTC) - timedelta(days=5)
    saga_id = await _seed_saga(
        sessionmaker_, state=LifecycleState.RETURNED, updated_at=recent
    )
    scrubber = PatronScrubber(salt=SALT)
    scanner = RetentionScanner(
        sessionmaker=sessionmaker_,
        scrubber=scrubber,
        retention_days=90,
        interval_secs=3600.0,
    )
    results = await scanner.scan()
    assert results == []

    async with sessionmaker_() as session:
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        assert not is_scrubbed(saga.request_payload["patron"]["patron_id"])


async def test_scanner_scrubs_eligible_saga(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal + past retention window = scrubbed on first pass."""
    long_ago = datetime.now(UTC) - timedelta(days=120)
    saga_id = await _seed_saga(
        sessionmaker_, state=LifecycleState.RETURNED, updated_at=long_ago
    )
    scrubber = PatronScrubber(salt=SALT)
    scanner = RetentionScanner(
        sessionmaker=sessionmaker_,
        scrubber=scrubber,
        retention_days=90,
        interval_secs=3600.0,
    )
    results = await scanner.scan()
    assert any(r.scrubbed for r in results if r.saga_id == saga_id)

    async with sessionmaker_() as session:
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        assert is_scrubbed(saga.request_payload["patron"]["patron_id"])


async def test_scanner_replay_is_noop(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Two scans back-to-back: second writes no new events."""
    long_ago = datetime.now(UTC) - timedelta(days=120)
    saga_id = await _seed_saga(
        sessionmaker_, state=LifecycleState.RETURNED, updated_at=long_ago
    )
    scrubber = PatronScrubber(salt=SALT)
    scanner = RetentionScanner(
        sessionmaker=sessionmaker_,
        scrubber=scrubber,
        retention_days=90,
        interval_secs=3600.0,
    )
    await scanner.scan()

    # Second scan re-queries the same saga (its updated_at was bumped by
    # the JSONB mutation; SQLAlchemy may or may not set onupdate=now in
    # test SQLite). Either way, the scrubber's "already scrubbed" check
    # short-circuits and no new event lands.
    await scanner.scan()

    async with sessionmaker_() as session:
        events = (
            (
                await session.execute(
                    select(SagaEventRow).where(
                        SagaEventRow.idempotency_key == f"patron_scrubbed-{saga_id}"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1  # exactly one, never duplicated


async def test_scrub_walks_saga_event_payloads(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Reviewer HIGH: cleartext patron_id baked into saga_event
    payloads (RECEIVE / RETURN forwards write it to NCIP intents)
    must be replaced by the scrub. Otherwise the retention policy
    leaks PII through the ledger."""
    saga_id = await _seed_saga(sessionmaker_, state=LifecycleState.RETURNED)

    # Hand-craft a saga_event with cleartext patron_id baked in payload
    # (the shape FORWARD events for RECEIVE produce in real flows).
    async with sessionmaker_() as session, session.begin():
        ev = SagaEventRow(
            saga_id=saga_id,
            seq=1,
            kind="forward",
            step="receive",
            state_before="shipped",
            state_after="received",
            actor="staff:alice@A",
            idempotency_key=f"event-{saga_id}-receive-test",
            payload={
                "outbox": {
                    "target": "ncip",
                    "args": {
                        "item_id": "BC-0001",
                        "patron_id": "patron-001",
                    },
                },
                "item_barcode": "BC-0001",
            },
            outcome="committed",
        )
        session.add(ev)

    scrubber = PatronScrubber(salt=SALT)
    async with sessionmaker_() as session, session.begin():
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        await scrubber.scrub(session, saga)

    async with sessionmaker_() as session:
        rows = (
            (
                await session.execute(
                    select(SagaEventRow).where(SagaEventRow.saga_id == saga_id)
                )
            )
            .scalars()
            .all()
        )
        receive_ev = next(e for e in rows if e.step == "receive")
        nested_patron = receive_ev.payload["outbox"]["args"]["patron_id"]
        assert nested_patron.startswith(SCRUBBED_PREFIX)
        assert nested_patron != "patron-001"
        assert receive_ev.payload["item_barcode"] is None


async def test_scrub_walks_outbox_payloads(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Reviewer HIGH: outbox rows queued by RECEIVE / RETURN carry
    cleartext patron_id until delivered. A scrub before delivery must
    also rewrite the queue payload."""
    from agora.saga.db import OutboxRow

    saga_id = await _seed_saga(sessionmaker_, state=LifecycleState.RETURNED)

    async with sessionmaker_() as session, session.begin():
        row = OutboxRow(
            saga_id=saga_id,
            target="ncip",
            idempotency_key=f"outbox-{saga_id}-test",
            payload={
                "action": "check_out",
                "args": {
                    "item_id": "BC-0001",
                    "patron_id": "patron-001",
                },
            },
            status="pending",
        )
        session.add(row)

    scrubber = PatronScrubber(salt=SALT)
    async with sessionmaker_() as session, session.begin():
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        await scrubber.scrub(session, saga)

    async with sessionmaker_() as session:
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
        nested_patron = rows[0].payload["args"]["patron_id"]
        assert nested_patron.startswith(SCRUBBED_PREFIX)


async def _seed_no_patron_saga(
    sm: async_sessionmaker[AsyncSession],
    *,
    state: LifecycleState,
    updated_at: datetime | None = None,
) -> UUID:
    """Saga whose payload carries no patron_id at all."""
    saga_id = uuid4()
    async with sm() as session, session.begin():
        saga = Saga(
            id=saga_id,
            request_id=uuid4(),
            current_state=state.value,
            request_payload={
                "request_type": "loan",
                "requesting_library": {"symbol": "A", "name": "Library A"},
                "item": {"title": "Brave New World"},
            },
        )
        if updated_at is not None:
            saga.updated_at = updated_at
        session.add(saga)
    return saga_id


def _make_scanner(
    sm: async_sessionmaker[AsyncSession], *, batch_size: int = 500
) -> RetentionScanner:
    return RetentionScanner(
        sessionmaker=sm,
        scrubber=PatronScrubber(salt=SALT),
        retention_days=90,
        interval_secs=3600.0,
        batch_size=batch_size,
    )


async def test_scanner_excludes_already_scrubbed_in_sql(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Reviewer HIGH: a scrubbed saga must not re-enter the scan window.

    Pre-fix the scanner re-selected scrubbed sagas forever (scrub
    early-returns without bumping ``updated_at``); the exclusion now
    lives in SQL, so a second scan returns NO result for the saga at
    all (not even a ``scrubbed=False`` no-op row).
    """
    long_ago = datetime.now(UTC) - timedelta(days=120)
    saga_id = await _seed_saga(
        sessionmaker_, state=LifecycleState.RETURNED, updated_at=long_ago
    )
    scanner = _make_scanner(sessionmaker_)

    first = await scanner.scan()
    assert [r.saga_id for r in first] == [saga_id]
    assert first[0].scrubbed is True

    second = await scanner.scan()
    assert second == []  # excluded by the SQL predicate, not a no-op pass


async def test_scanner_scrubbed_backlog_does_not_starve_unscrubbed(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """A scrubbed backlog larger than the batch window can't starve a
    never-scrubbed saga: scrubbed rows are excluded in SQL, so the
    bounded window only ever contains real work."""
    long_ago = datetime.now(UTC) - timedelta(days=120)
    backlog: list[UUID] = []
    for i in range(3):
        backlog.append(
            await _seed_saga(
                sessionmaker_,
                state=LifecycleState.RETURNED,
                patron_id=f"patron-backlog-{i}",
                updated_at=long_ago,
            )
        )
    # Pre-scrub the backlog directly.
    scrubber = PatronScrubber(salt=SALT)
    async with sessionmaker_() as session, session.begin():
        for sid in backlog:
            saga = await session.get(Saga, sid)
            assert saga is not None
            await scrubber.scrub(session, saga)

    # Batch window (2) smaller than the scrubbed backlog (3): the
    # arbitrary-window starvation bug would fill the window with
    # scrubbed rows and never reach the fresh saga.
    fresh_id = await _seed_saga(
        sessionmaker_,
        state=LifecycleState.RETURNED,
        patron_id="patron-fresh",
        updated_at=long_ago,
    )
    scanner = _make_scanner(sessionmaker_, batch_size=2)
    results = await scanner.scan()
    assert [r.saga_id for r in results] == [fresh_id]
    assert results[0].scrubbed is True


async def test_scanner_excludes_sagas_without_patron_id(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """No-patron sagas carry nothing to scrub and must not re-select
    forever (scrub() would no-op them without ever mutating the row)."""
    long_ago = datetime.now(UTC) - timedelta(days=120)
    await _seed_no_patron_saga(
        sessionmaker_, state=LifecycleState.RETURNED, updated_at=long_ago
    )
    scanner = _make_scanner(sessionmaker_)
    assert await scanner.scan() == []


# ---------------------------------------------------------------------
# Eligibility clock — terminal-transition event ts, not updated_at
# ---------------------------------------------------------------------


async def _add_event(
    sm: async_sessionmaker[AsyncSession],
    *,
    saga_id: UUID,
    seq: int,
    state_after: str,
    ts: datetime,
    outcome: str = "committed",
    kind: str = "forward",
    step: str = "return",
) -> None:
    async with sm() as session, session.begin():
        session.add(
            SagaEventRow(
                saga_id=saga_id,
                seq=seq,
                kind=kind,
                step=step,
                state_before="received",
                state_after=state_after,
                actor="staff:test",
                idempotency_key=f"event-{saga_id}-{seq}",
                payload={},
                outcome=outcome,
                ts=ts,
            )
        )


async def test_scanner_uses_terminal_event_ts_not_updated_at(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Reviewer MED: ``updated_at`` bumps on ANY row touch (late
    OBSERVATION, tracking refresh) and would restart the retention
    clock past the statutory deadline. Eligibility keys off the ts of
    the committed terminal-transition event instead: terminal for
    >retention_days but updated_at recent → still scrubbed."""
    saga_id = await _seed_saga(
        sessionmaker_,
        state=LifecycleState.RETURNED,
        updated_at=datetime.now(UTC),  # freshly touched
    )
    await _add_event(
        sessionmaker_,
        saga_id=saga_id,
        seq=1,
        state_after=LifecycleState.RETURNED.value,
        ts=datetime.now(UTC) - timedelta(days=120),  # terminal long ago
    )
    scanner = _make_scanner(sessionmaker_)
    results = await scanner.scan()
    assert [r.saga_id for r in results] == [saga_id]
    assert results[0].scrubbed is True


async def test_scanner_recent_terminal_event_defers_scrub(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Converse guard: an old ``updated_at`` with a RECENT terminal
    transition must NOT be scrubbed — the retention window starts at
    the terminal transition, not the last row write."""
    saga_id = await _seed_saga(
        sessionmaker_,
        state=LifecycleState.RETURNED,
        updated_at=datetime.now(UTC) - timedelta(days=120),
    )
    await _add_event(
        sessionmaker_,
        saga_id=saga_id,
        seq=1,
        state_after=LifecycleState.RETURNED.value,
        ts=datetime.now(UTC) - timedelta(days=5),  # terminal 5 days ago
    )
    scanner = _make_scanner(sessionmaker_)
    assert await scanner.scan() == []

    async with sessionmaker_() as session:
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        assert not is_scrubbed(saga.request_payload["patron"]["patron_id"])


# ---------------------------------------------------------------------
# NCIP item_id barcode scrub (flows.py stores item_barcode as item_id)
# ---------------------------------------------------------------------


async def test_scrub_nulls_item_id_equal_to_barcode(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """Reviewer MED: RECEIVE/RETURN NCIP payloads store the ILS barcode
    under ``item_id`` — scrub must null it when it equals the saga's
    cleartext barcode, but preserve the reshare_id fallback."""
    saga_id = await _seed_saga(
        sessionmaker_, state=LifecycleState.RETURNED, item_barcode="BC-0001"
    )
    async with sessionmaker_() as session, session.begin():
        session.add(
            SagaEventRow(
                saga_id=saga_id,
                seq=1,
                kind="forward",
                step="receive",
                state_before="shipped",
                state_after="received",
                actor="staff:test",
                idempotency_key=f"event-{saga_id}-barcode",
                payload={
                    "outbox": {
                        "target": "ncip",
                        "args": {"item_id": "BC-0001", "patron_id": "patron-001"},
                    },
                },
                outcome="committed",
            )
        )
        session.add(
            SagaEventRow(
                saga_id=saga_id,
                seq=2,
                kind="forward",
                step="return",
                state_before="received",
                state_after="returned",
                actor="staff:test",
                idempotency_key=f"event-{saga_id}-fallback",
                payload={
                    "outbox": {
                        "target": "ncip",
                        # reshare_id fallback shape (no barcode on request)
                        "args": {"item_id": "RS-42", "patron_id": "patron-001"},
                    },
                },
                outcome="committed",
            )
        )

    scrubber = PatronScrubber(salt=SALT)
    async with sessionmaker_() as session, session.begin():
        saga = await session.get(Saga, saga_id)
        assert saga is not None
        await scrubber.scrub(session, saga)

    async with sessionmaker_() as session:
        rows = (
            (
                await session.execute(
                    select(SagaEventRow).where(SagaEventRow.saga_id == saga_id)
                )
            )
            .scalars()
            .all()
        )
        by_step = {e.step: e for e in rows if e.step in {"receive", "return"}}
        # Barcode-valued item_id nulled.
        assert by_step["receive"].payload["outbox"]["args"]["item_id"] is None
        # reshare_id fallback preserved.
        assert by_step["return"].payload["outbox"]["args"]["item_id"] == "RS-42"


def test_scanner_rejects_bad_construction() -> None:
    scrubber = PatronScrubber(salt=SALT)
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker()
    with pytest.raises(ValueError, match="retention_days"):
        RetentionScanner(
            sessionmaker=sm,
            scrubber=scrubber,
            retention_days=-1,
            interval_secs=3600.0,
        )
    with pytest.raises(ValueError, match="interval_secs"):
        RetentionScanner(
            sessionmaker=sm,
            scrubber=scrubber,
            retention_days=90,
            interval_secs=0.0,
        )
