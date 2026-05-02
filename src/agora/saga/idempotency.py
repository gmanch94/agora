"""Idempotency primitives.

ULID-based key generation, plus inbox/outbox helpers. The ledger's
``UNIQUE(idempotency_key)`` constraint provides the actual replay-safety
guarantee; this module provides the primitives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
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
    """Read pending outbox rows ordered by schedule time."""
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


async def outbox_mark_delivered(session: AsyncSession, row_id: int) -> None:
    """Mark a row delivered."""
    row = await session.get(OutboxRow, row_id)
    if row is None:
        return
    row.status = "delivered"
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
    """Increment attempts; mark dead-letter after ``max_attempts``."""
    row = await session.get(OutboxRow, row_id)
    if row is None:
        return
    row.attempts += 1
    row.last_error = error[:2048]
    if row.attempts >= max_attempts:
        row.status = "dead_letter"
    elif requeue_after_secs is not None:
        from datetime import timedelta

        row.scheduled_for = datetime.now(UTC) + timedelta(seconds=requeue_after_secs)
    await session.flush()
