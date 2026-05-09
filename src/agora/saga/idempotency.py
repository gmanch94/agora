"""Idempotency primitives.

ULID-based key generation, plus inbox/outbox helpers. The ledger's
``UNIQUE(idempotency_key)`` constraint provides the actual replay-safety
guarantee; this module provides the primitives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from agora.saga.db import InboxRow, OutboxRow


def new_idempotency_key(prefix: str | None = None) -> str:
    """Generate a fresh ULID-based idempotency key.

    Optional ``prefix`` (e.g. ``"submit"``) makes keys easier to grep
    in logs without affecting uniqueness.
    """
    ulid = str(ULID())
    return f"{prefix}-{ulid}" if prefix else ulid


async def inbox_seen(session: AsyncSession, message_id: str) -> InboxRow | None:
    """Return the existing inbox row for ``message_id``, or ``None``."""
    return await session.get(InboxRow, message_id)


async def inbox_record(
    session: AsyncSession,
    *,
    message_id: str,
    source: str,
    response: dict[str, Any] | None = None,
) -> InboxRow:
    """Insert an inbox row.

    Caller is responsible for the surrounding transaction. If the row
    already exists, returns the existing row without modification.
    """
    existing = await inbox_seen(session, message_id)
    if existing is not None:
        return existing
    row = InboxRow(message_id=message_id, source=source, response=response)
    session.add(row)
    await session.flush()
    return row


async def outbox_enqueue(
    session: AsyncSession,
    *,
    saga_id: Any,
    target: str,
    idempotency_key: str,
    payload: dict[str, Any],
    scheduled_for: datetime | None = None,
) -> OutboxRow:
    """Append an outbox row.

    The outbox worker (separate process / task) drives delivery.
    Returning the row is convenient for tests and tracing.
    """
    row = OutboxRow(
        saga_id=saga_id,
        target=target,
        idempotency_key=idempotency_key,
        payload=payload,
        status="pending",
        attempts=0,
        scheduled_for=scheduled_for or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def outbox_pending(session: AsyncSession, *, limit: int = 100) -> list[OutboxRow]:
    """Read pending outbox rows ordered by schedule time.

    Read-only — does NOT claim. Two concurrent workers calling this
    will see overlapping rows and double-deliver. Production callers
    must use :func:`outbox_claim` instead. Kept for tests and callers
    that legitimately want a peek without side-effects.
    """
    now = datetime.now(UTC)
    stmt = (
        select(OutboxRow)
        .where(OutboxRow.status == "pending")
        .where(OutboxRow.scheduled_for <= now)
        .order_by(OutboxRow.scheduled_for.asc(), OutboxRow.id.asc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def outbox_claim(
    session: AsyncSession,
    *,
    limit: int = 100,
    lease_secs: int = 600,
) -> list[OutboxRow]:
    """Atomically claim up to ``limit`` ready rows for dispatch.

    Multi-worker safe on Postgres via ``SELECT ... FOR UPDATE SKIP
    LOCKED``: two concurrent workers each claim disjoint row sets and
    neither blocks the other. SQLite ignores the locking hint (the
    driver serializes writers anyway) so the same query path works
    in tests.

    Pattern (Postgres):

        1. Sweep stale ``in_flight`` rows back to ``pending`` if their
           ``claimed_at`` is older than ``lease_secs`` (orphan recovery
           from a worker that crashed mid-dispatch).
        2. ``SELECT ... FROM outbox WHERE status='pending' AND
           scheduled_for <= now() ORDER BY scheduled_for, id LIMIT N
           FOR UPDATE SKIP LOCKED`` — acquires row-level locks, skipping
           any row another worker already holds.
        3. Flip those rows to ``status='in_flight'`` with
           ``claimed_at = now()``. Caller commits the surrounding
           transaction, which both releases the locks AND publishes the
           ``in_flight`` flag so other workers' subsequent
           ``WHERE status='pending'`` skips them.

    The lease (``lease_secs``, default 600s) bounds how long an
    abandoned ``in_flight`` row stays out of rotation. It must exceed
    the longest plausible handler runtime, otherwise a slow handler
    will see its row reclaimed by a peer and double-delivered. The
    handler-level idempotency-key contract still protects correctness
    in that race; the lease just keeps it rare.

    Caller is responsible for the surrounding transaction. The claim
    only takes effect once the caller commits.
    """
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=lease_secs)

    # Step 1: orphan recovery. Reclaim any in_flight row whose lease
    # has expired. Cheap: an indexed status filter + a timestamp
    # comparison; nothing to do most passes.
    await session.execute(
        update(OutboxRow)
        .where(OutboxRow.status == "in_flight")
        .where(OutboxRow.claimed_at < stale_cutoff)
        .values(status="pending", claimed_at=None)
    )

    # Step 2: acquire row locks on ready rows. ``with_for_update`` maps
    # to ``FOR UPDATE`` on Postgres; ``skip_locked=True`` adds ``SKIP
    # LOCKED`` so peers don't block. SQLite's driver ignores the hint
    # and serializes writers naturally, which is fine for tests.
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    select_stmt = (
        select(OutboxRow)
        .where(OutboxRow.status == "pending")
        .where(OutboxRow.scheduled_for <= now)
        .order_by(OutboxRow.scheduled_for.asc(), OutboxRow.id.asc())
        .limit(limit)
    )
    if dialect == "postgresql":
        select_stmt = select_stmt.with_for_update(skip_locked=True)
    rows = list((await session.execute(select_stmt)).scalars().all())

    # Step 3: flip to in_flight + stamp lease. The UPDATE happens while
    # we still hold the row locks (caller's transaction is open), so
    # the flip is published atomically with lock release at commit.
    for row in rows:
        row.status = "in_flight"
        row.claimed_at = now
    await session.flush()
    return rows


def _normalize_dt_for_compare(dt: datetime | None) -> datetime | None:
    """Strip tz to UTC-naive for cross-session comparison.

    SQLAlchemy + aiosqlite drops tzinfo on round-trip even with
    ``DateTime(timezone=True)``: writes a tz-aware datetime as a UTC
    ISO string and reads it back tz-naive. Comparing the in-memory
    snapshot (tz-aware) against a fresh-session read (tz-naive) would
    always be False on SQLite. Postgres preserves tz, so this is a
    no-op there. Normalize both sides so the comparator is portable.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


async def outbox_claim_still_ours(
    session: AsyncSession, row_id: int, expected_claimed_at: datetime
) -> bool:
    """Return True iff row ``row_id`` is still claimed at ``expected_claimed_at``.

    Used by the outbox worker just before ``mark_delivered`` to detect
    the lease-race window: worker A claims a row, A's handler hangs
    past ``claim_lease_secs``, B's orphan-recovery sweep flips the row
    back to ``pending`` and B re-claims it with a fresh ``claimed_at``.
    Without this check, A would then write ``mark_delivered`` and the
    on_success projection while B's HTTP call is also in flight —
    duplicate supplier-side actions follow because mod-rs does not
    honour ``Idempotency-Key`` (per ``clients/reshare.py`` module
    docstring).

    Returning False tells the worker: drop the result silently and
    let B's call win. The supplier-side duplication is not avoided —
    both calls already went out — but at least the projection and
    ledger don't double-write.

    Audit 2026-05-09 #12.
    """
    row = await session.get(OutboxRow, row_id)
    if row is None:
        return False
    if row.status != "in_flight":
        return False
    return _normalize_dt_for_compare(row.claimed_at) == _normalize_dt_for_compare(
        expected_claimed_at
    )


async def outbox_release_claim(session: AsyncSession, row_id: int) -> None:
    """Release a claim without recording a delivery or failure.

    Used when the worker decides the row can't be processed right now
    (e.g. no handler is registered for ``target``) but no failure has
    occurred. Reverts ``in_flight → pending`` and clears
    ``claimed_at`` so the row stays eligible for the next pass without
    burning an attempt or waiting for the orphan-recovery lease to
    expire. ``attempts`` is left unchanged.
    """
    row = await session.get(OutboxRow, row_id)
    if row is None:
        return
    if row.status != "in_flight":
        return
    row.status = "pending"
    row.claimed_at = None
    await session.flush()


async def outbox_mark_delivered(session: AsyncSession, row_id: int) -> None:
    """Mark a row delivered. Clears ``claimed_at`` for tidiness."""
    row = await session.get(OutboxRow, row_id)
    if row is None:
        return
    row.status = "delivered"
    row.claimed_at = None
    row.delivered_at = datetime.now(UTC)
    await session.flush()


async def outbox_mark_failed(
    session: AsyncSession,
    row_id: int,
    *,
    error: str,
    requeue_after_secs: int | None = None,
    max_attempts: int = 10,
) -> None:
    """Increment attempts; release the claim (in_flight → pending or dead_letter).

    The row enters this function in ``status='in_flight'`` (claimed by
    the worker that's now reporting failure). On retry we flip it back
    to ``pending`` and clear ``claimed_at`` so the next drain pass can
    re-claim it. On terminal failure we flip to ``dead_letter`` and
    also clear ``claimed_at`` (no further claims expected).
    """
    row = await session.get(OutboxRow, row_id)
    if row is None:
        return
    row.attempts += 1
    row.last_error = error[:2048]
    if row.attempts >= max_attempts:
        row.status = "dead_letter"
        row.claimed_at = None
    else:
        row.status = "pending"
        row.claimed_at = None
        if requeue_after_secs is not None:
            row.scheduled_for = datetime.now(UTC) + timedelta(seconds=requeue_after_secs)
    await session.flush()
