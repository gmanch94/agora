"""Idempotency primitives — ULID generation, inbox, outbox."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agora.saga.idempotency import (
    inbox_record,
    inbox_seen,
    new_idempotency_key,
    outbox_enqueue,
    outbox_mark_delivered,
    outbox_mark_failed,
    outbox_pending,
)


def test_new_idempotency_key_unique() -> None:
    keys = {new_idempotency_key() for _ in range(1000)}
    assert len(keys) == 1000


def test_new_idempotency_key_with_prefix_starts_with_prefix() -> None:
    key = new_idempotency_key(prefix="approve")
    assert key.startswith("approve-")
    assert len(key) > len("approve-") + 10


@pytest.mark.asyncio
async def test_inbox_dedupes_same_message_id(session) -> None:
    async with session.begin():
        first = await inbox_record(
            session, message_id="m-1", source="reshare", response={"ok": True}
        )
    assert first.message_id == "m-1"

    async with session.begin():
        seen = await inbox_seen(session, "m-1")
    assert seen is not None
    assert seen.message_id == "m-1"

    # Replay attempt: should not duplicate.
    async with session.begin():
        replay = await inbox_record(
            session, message_id="m-1", source="reshare", response={"ok": False}
        )
    assert replay.message_id == "m-1"
    # original response preserved (existing row returned, not overwritten).
    assert replay.response == {"ok": True}


@pytest.mark.asyncio
async def test_outbox_pending_then_delivered(session) -> None:
    saga_id = uuid4()
    async with session.begin():
        row = await outbox_enqueue(
            session,
            saga_id=saga_id,
            target="reshare",
            idempotency_key=new_idempotency_key(),
            payload={"action": "send"},
        )
        row_id = row.id

    async with session.begin():
        pending = await outbox_pending(session)
    assert any(r.id == row_id for r in pending)

    async with session.begin():
        await outbox_mark_delivered(session, row_id)

    async with session.begin():
        pending2 = await outbox_pending(session)
    assert all(r.id != row_id for r in pending2)


@pytest.mark.asyncio
async def test_outbox_mark_failed_dead_letters_after_max_attempts(session) -> None:
    saga_id = uuid4()
    async with session.begin():
        row = await outbox_enqueue(
            session,
            saga_id=saga_id,
            target="reshare",
            idempotency_key=new_idempotency_key(),
            payload={},
        )
        row_id = row.id

    for i in range(3):
        async with session.begin():
            await outbox_mark_failed(
                session,
                row_id,
                error=f"attempt {i}",
                requeue_after_secs=None,
                max_attempts=3,
            )

    async with session.begin():
        from agora.saga.db import OutboxRow

        refreshed = await session.get(OutboxRow, row_id)
    assert refreshed is not None
    assert refreshed.status == "dead_letter"
    assert refreshed.attempts == 3
