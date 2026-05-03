"""Multi-worker outbox safety against real Postgres (backlog #5).

Verifies the ``SELECT ... FOR UPDATE SKIP LOCKED`` claim path in
:func:`agora.saga.idempotency.outbox_claim`: two workers draining the
same outbox table in parallel must each claim disjoint row sets and
neither double-deliver any row.

Skipped unless ``AGORA_TEST_DB_URL`` is set; CI sets it to the
postgres service container in ``.github/workflows/postgres-tests.yml``.

Why this test needs a real Postgres
-----------------------------------
SQLite serializes writers via a database-level lock — two concurrent
workers against SQLite never *actually* race because the second one
blocks on the first's write transaction. The whole point of
``FOR UPDATE SKIP LOCKED`` is row-level locking, which only Postgres
(and a few other RDBMSes) implement. So a SQLite-only test would pass
even if the claim logic were broken; we need a real Postgres to
exercise the contention path.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from agora.saga.db import OutboxRow
from agora.saga.idempotency import outbox_enqueue
from agora.saga.outbox import OutboxWorker

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ALEMBIC_DIR = _REPO_ROOT / "alembic"

_TEST_DB_URL = os.environ.get("AGORA_TEST_DB_URL")

requires_pg = pytest.mark.skipif(
    _TEST_DB_URL is None,
    reason="AGORA_TEST_DB_URL not set; spin up a Postgres and re-run",
)


def _alembic_config(url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    return cfg


async def _upgrade(url: str) -> None:
    """Run ``alembic upgrade head`` in a worker thread.

    Same trick as ``test_alembic_postgres.py``: alembic's env.py calls
    ``asyncio.run`` internally, so dispatching through
    ``asyncio.to_thread`` gives it a fresh OS thread + loop.
    """
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "head")


@pytest_asyncio.fixture
async def fresh_pg_engine() -> AsyncIterator[AsyncEngine]:
    """Drop everything in ``public`` and yield a clean async engine."""
    assert _TEST_DB_URL is not None  # nosec B101  # gated by requires_pg
    engine = create_async_engine(_TEST_DB_URL, future=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    try:
        yield engine
    finally:
        await engine.dispose()


@requires_pg
async def test_concurrent_workers_no_double_delivery(
    fresh_pg_engine: AsyncEngine,
) -> None:
    """Two workers draining in parallel deliver each row exactly once.

    Without ``FOR UPDATE SKIP LOCKED`` both workers would read the
    same pending rows, both call the handler, and stats.delivered
    would total > rows enqueued. With the claim pattern they see
    disjoint row sets and the totals add up cleanly.
    """
    assert _TEST_DB_URL is not None  # nosec B101  # gated by requires_pg
    await _upgrade(_TEST_DB_URL)

    sm = async_sessionmaker(bind=fresh_pg_engine, expire_on_commit=False)

    # Enqueue a meaningful batch so both workers find work on first
    # claim. A batch of 30 against limit=50 is enough that one worker
    # could in principle claim everything; the contention only shows up
    # if both workers happen to call ``outbox_claim`` at overlapping
    # times. asyncio.gather + the handler's tiny sleep make that very
    # likely.
    n_rows = 30
    async with sm() as s:
        for i in range(n_rows):
            await outbox_enqueue(
                s,
                saga_id=uuid4(),
                target="t1",
                idempotency_key=f"concurrent-{i}",
                payload={"i": i},
            )
        await s.commit()

    delivered: list[str] = []
    deliver_lock = asyncio.Lock()

    async def slow_handler(payload: dict[str, Any], idem: str) -> None:
        # Tiny sleep widens the window where a peer could mis-claim
        # the same row. The lock-protected append is so we can assert
        # the final list contents without races in the test itself.
        await asyncio.sleep(0.02)
        async with deliver_lock:
            delivered.append(idem)

    worker_a = OutboxWorker(sm, {"t1": slow_handler})
    worker_b = OutboxWorker(sm, {"t1": slow_handler})

    stats_a, stats_b = await asyncio.gather(
        worker_a.drain_until_empty(limit=10),
        worker_b.drain_until_empty(limit=10),
    )

    # Exactly-once delivery: each enqueued row delivered to exactly
    # one worker, and the totals match what was enqueued.
    assert stats_a.delivered + stats_b.delivered == n_rows, (
        f"deliveries don't match enqueue count: "
        f"a={stats_a.delivered}, b={stats_b.delivered}, expected {n_rows}"
    )
    assert stats_a.failed == 0
    assert stats_b.failed == 0
    assert stats_a.dead_letter == 0
    assert stats_b.dead_letter == 0

    # The set of idempotency keys delivered = the set we enqueued, no
    # duplicates and no misses.
    expected = {f"concurrent-{i}" for i in range(n_rows)}
    assert set(delivered) == expected
    assert len(delivered) == n_rows  # no duplicate calls

    # Both workers actually did some work (sanity: if one worker
    # vacuumed everything before the other started, the test isn't
    # exercising contention). Allow a wide imbalance — scheduling is
    # non-deterministic — but flag a totally one-sided run.
    assert stats_a.delivered >= 1, "worker A drained zero rows — re-tune timing"
    assert stats_b.delivered >= 1, "worker B drained zero rows — re-tune timing"

    # Verify final DB state: every row is delivered, none in_flight.
    async with fresh_pg_engine.connect() as conn:
        result = await conn.execute(
            select(OutboxRow.status, OutboxRow.idempotency_key)
        )
        rows = result.fetchall()
    statuses = {r[0] for r in rows}
    assert statuses == {"delivered"}, f"non-delivered rows linger: {rows}"


@requires_pg
async def test_concurrent_claims_are_disjoint(
    fresh_pg_engine: AsyncEngine,
) -> None:
    """Direct test of :func:`outbox_claim` under contention.

    Two parallel claims against the same engine must return disjoint
    row sets. This is the primitive the worker depends on; if it
    breaks, the higher-level no-double-delivery test breaks too —
    but exercising it directly localises failures.
    """
    assert _TEST_DB_URL is not None  # nosec B101  # gated by requires_pg
    await _upgrade(_TEST_DB_URL)

    sm = async_sessionmaker(bind=fresh_pg_engine, expire_on_commit=False)
    n_rows = 20
    async with sm() as s:
        for i in range(n_rows):
            await outbox_enqueue(
                s,
                saga_id=uuid4(),
                target="t1",
                idempotency_key=f"disjoint-{i}",
                payload={"i": i},
            )
        await s.commit()

    from agora.saga.idempotency import outbox_claim

    async def claim_batch() -> list[int]:
        async with sm() as s, s.begin():
            rows = await outbox_claim(s, limit=n_rows, lease_secs=600)
            # Snapshot ids before commit/close detaches the rows.
            return [r.id for r in rows]

    a_ids, b_ids = await asyncio.gather(claim_batch(), claim_batch())

    # Disjoint: no row appears in both claims.
    overlap = set(a_ids) & set(b_ids)
    assert not overlap, f"both workers claimed the same rows: {overlap}"

    # Together they accounted for every row (or at least, no row was
    # left unclaimed AND not in_flight). With limit >= n_rows and
    # SKIP LOCKED, the union should equal all n_rows.
    assert len(set(a_ids) | set(b_ids)) == n_rows, (
        f"some rows escaped both claims: "
        f"a={len(a_ids)}, b={len(b_ids)}, expected {n_rows}"
    )


@requires_pg
async def test_in_flight_row_invisible_to_pending_select(
    fresh_pg_engine: AsyncEngine,
) -> None:
    """A claimed row's ``in_flight`` status hides it from the next claim.

    Belt-and-braces: even after the claim transaction commits and
    locks release, a peer worker calling ``outbox_claim`` should see
    zero ready rows because the WHERE clause filters
    ``status='pending'``.
    """
    assert _TEST_DB_URL is not None  # nosec B101  # gated by requires_pg
    await _upgrade(_TEST_DB_URL)

    sm = async_sessionmaker(bind=fresh_pg_engine, expire_on_commit=False)
    async with sm() as s:
        await outbox_enqueue(
            s,
            saga_id=uuid4(),
            target="t1",
            idempotency_key="invisible-1",
            payload={},
        )
        await s.commit()

    from agora.saga.idempotency import outbox_claim

    # First claim takes the row.
    async with sm() as s, s.begin():
        first = await outbox_claim(s, limit=10, lease_secs=600)
    assert len(first) == 1

    # Second claim from a fresh session sees nothing — row is in_flight.
    async with sm() as s, s.begin():
        second = await outbox_claim(s, limit=10, lease_secs=600)
    assert second == []
