"""Real-Postgres Alembic test (backlog #4).

Exercises the Alembic migration path against a live Postgres so we
catch drift between ``alembic/versions/`` and the ORM in
``saga/db.py``. Skipped unless ``AGORA_TEST_DB_URL`` is set; CI sets
it to the workflow's postgres service.

Three assertions, three test functions:

1. :func:`test_alembic_upgrade_head_succeeds` — ``alembic upgrade
   head`` runs to completion against a clean DB.

2. :func:`test_alembic_round_trip` — ``upgrade head`` → ``downgrade
   base`` → ``upgrade head`` cycles cleanly. Catches a downgrade that
   forgets to drop something the next upgrade would conflict with.

3. :func:`test_orm_matches_migrated_schema` — after upgrade, the live
   schema matches ``Base.metadata`` per
   :func:`alembic.autogenerate.compare_metadata`. A trivial filter
   drops cosmetic noise (server_default text-vs-FunctionElement) so
   the assertion fires only on real divergence (a missing column,
   a renamed table, a forgotten constraint).

Run locally with::

    docker run --rm -d --name agora-test-pg -p 55432:5432 \\
      -e POSTGRES_USER=agora -e POSTGRES_PASSWORD=agora \\
      -e POSTGRES_DB=agora_test postgres:15-alpine
    AGORA_TEST_DB_URL=postgresql+asyncpg://agora:agora@localhost:55432/agora_test \\
      pytest tests/test_alembic_postgres.py -v

Note on event loops: ``alembic.command.upgrade`` is synchronous and
``alembic/env.py`` runs ``asyncio.run(run_migrations_online())``
internally. Calling that from inside an already-running event loop
raises ``RuntimeError: asyncio.run() cannot be called from a running
event loop``. We side-step it by dispatching ``command.upgrade`` /
``command.downgrade`` through :func:`asyncio.to_thread`, which gives
alembic a fresh OS thread (and therefore a fresh loop) to call
``asyncio.run`` on.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agora.saga.db import Base

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ALEMBIC_DIR = _REPO_ROOT / "alembic"

_TEST_DB_URL = os.environ.get("AGORA_TEST_DB_URL")
_HEAD_REVISION = "20260509_saga_patron_id_index"

requires_pg = pytest.mark.skipif(
    _TEST_DB_URL is None,
    reason="AGORA_TEST_DB_URL not set; spin up a Postgres and re-run",
)


def _alembic_config(url: str) -> Config:
    """Build a programmatic Alembic Config pointing at ``url``.

    ``alembic/env.py`` honors an already-set ``sqlalchemy.url``, so
    the override here flows through the async migration path without
    touching ``agora.config``.
    """
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    return cfg


async def _upgrade(url: str, target: str = "head") -> None:
    """Run ``alembic upgrade <target>`` in a worker thread.

    See module docstring for why this can't run inline.
    """
    await asyncio.to_thread(command.upgrade, _alembic_config(url), target)


async def _downgrade(url: str, target: str) -> None:
    """Run ``alembic downgrade <target>`` in a worker thread."""
    await asyncio.to_thread(command.downgrade, _alembic_config(url), target)


@pytest_asyncio.fixture
async def fresh_pg_engine() -> AsyncIterator[AsyncEngine]:
    """Drop everything in ``public`` and yield a clean async engine.

    A bare ``DROP SCHEMA public CASCADE`` resets the DB to empty. Each
    test starts from zero so ordering between tests doesn't matter.
    """
    assert _TEST_DB_URL is not None  # nosec B101  # gated by requires_pg
    engine = create_async_engine(_TEST_DB_URL, future=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    try:
        yield engine
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@requires_pg
async def test_alembic_upgrade_head_succeeds(fresh_pg_engine: AsyncEngine) -> None:
    """`alembic upgrade head` runs to completion on a clean Postgres."""
    assert _TEST_DB_URL is not None  # nosec B101  # gated by requires_pg
    await _upgrade(_TEST_DB_URL)

    # Sanity: alembic_version row exists and points at head.
    async with fresh_pg_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT version_num FROM alembic_version")
        )
        rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == _HEAD_REVISION


@requires_pg
async def test_alembic_round_trip(fresh_pg_engine: AsyncEngine) -> None:
    """`upgrade head -> downgrade base -> upgrade head` is clean.

    Catches downgrades that forget to drop something the next upgrade
    would collide with (the no-op marker won't catch much today, but
    pins the contract for future revisions).
    """
    assert _TEST_DB_URL is not None  # nosec B101  # gated by requires_pg
    await _upgrade(_TEST_DB_URL)
    await _downgrade(_TEST_DB_URL, "base")

    # After base, only alembic_version remains; user tables are gone.
    async with fresh_pg_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name <> 'alembic_version' "
                "ORDER BY table_name"
            )
        )
        leftover = [row[0] for row in result.fetchall()]
    assert leftover == [], f"downgrade left tables behind: {leftover}"

    # And we can roll forward again.
    await _upgrade(_TEST_DB_URL)


# Indexes the ORM metadata deliberately does NOT declare because they
# are dialect-specific (Postgres-only GIN expression indexes whose
# SQLAlchemy representation is awkward in portable __table_args__).
# Alembic creates them via raw SQL in the migration; compare_metadata
# would otherwise flag them as "remove_index" diffs against the ORM.
_EXPECTED_DIALECT_ONLY_INDEXES: frozenset[str] = frozenset(
    {
        "ix_saga_patron_id",  # audit 2026-05-09 #37 (JSONB GIN)
    }
)


def _filter_compare_diffs(diffs: Iterable[Any]) -> list[Any]:
    """Drop cosmetic diffs that compare_metadata reports on a correct schema.

    Kept (real divergence):
      * ``add_table`` / ``remove_table``
      * ``add_column`` / ``remove_column``
      * ``add_constraint`` / ``remove_constraint`` (UNIQUE, FK, CHECK)
      * ``add_index`` / ``remove_index`` (except dialect-only indexes
        in :data:`_EXPECTED_DIALECT_ONLY_INDEXES`)

    Dropped (cosmetic):
      * ``modify_default`` — server_default text-vs-FunctionElement
        (``"now()"`` vs ``func.now()``) renders differently at the SQL
        level but is semantically identical for our schema.
      * ``modify_type`` when the existing and new types stringify the
        same — dialect variants occasionally trip this on ``VARCHAR``
        round-trips.
      * ``remove_index`` for dialect-only indexes — Postgres-specific
        GIN / expression indexes that ORM metadata can't portably
        declare without breaking SQLite ``create_all``.
    """
    kept: list[Any] = []
    for diff in diffs:
        # diff is either a single tuple (action, ...) or a list of them
        # (Alembic batches column-level diffs into a sub-list).
        if isinstance(diff, list):
            inner = _filter_compare_diffs(diff)
            if inner:
                kept.append(inner)
            continue

        action = diff[0] if diff else None
        if action == "modify_default":
            continue
        if action == "modify_type":
            existing = diff[5]
            new = diff[6]
            if str(existing) == str(new):
                continue
        if action == "remove_index":
            # diff shape: ("remove_index", Index(...))
            idx = diff[1] if len(diff) > 1 else None
            idx_name = getattr(idx, "name", None)
            if idx_name in _EXPECTED_DIALECT_ONLY_INDEXES:
                continue
        kept.append(diff)
    return kept


def _compare(connection: Connection) -> list[Any]:
    """Sync helper invoked via ``connection.run_sync``."""
    mc = MigrationContext.configure(connection)
    return list(compare_metadata(mc, Base.metadata))


@requires_pg
async def test_orm_matches_migrated_schema(fresh_pg_engine: AsyncEngine) -> None:
    """After ``upgrade head``, ORM metadata matches the live DB.

    Uses :func:`alembic.autogenerate.compare_metadata` to diff the
    declared ORM (``Base.metadata``) against the live schema. The
    filter in :func:`_filter_compare_diffs` drops cosmetic noise so
    the assertion fires only on real drift (a missing column, a
    renamed table, a forgotten constraint, a wrong type).
    """
    assert _TEST_DB_URL is not None  # nosec B101  # gated by requires_pg
    await _upgrade(_TEST_DB_URL)

    # compare_metadata wants a sync Connection. The async engine's
    # ``run_sync`` hands us one without requiring psycopg2 in deps.
    async with fresh_pg_engine.connect() as conn:
        raw = await conn.run_sync(_compare)

    diffs = _filter_compare_diffs(raw)
    assert diffs == [], (
        "ORM metadata diverges from migrated schema; "
        "either add an Alembic revision or fix the ORM. "
        f"Diffs:\n{diffs}"
    )
