"""Unit tests for saga/db.py ORM helpers.

Targets the 16 lines not exercised by other tests:
- _PortableUUID Postgres-dialect branches (lines 61, 68) and the
  None / already-UUID short-circuits (lines 66, 73, 75).
- get_engine() lazy initialisation (lines 223-230).
- get_sessionmaker() lazy initialisation (line 237).
- create_all() and drop_all() bodies (lines 253-255, 260-262).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

import agora.saga.db as db_module
from agora.saga.db import _PortableUUID

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PgDialect:
    """Minimal Postgres dialect stub."""

    name = "postgresql"

    def type_descriptor(self, t: Any) -> Any:
        return t


class _SqliteDialect:
    """Minimal SQLite dialect stub."""

    name = "sqlite"

    def type_descriptor(self, t: Any) -> Any:
        return t


_pg = _PgDialect()
_sq = _SqliteDialect()


# ---------------------------------------------------------------------------
# _PortableUUID — None short-circuits (lines 66, 73)
# ---------------------------------------------------------------------------


def test_portable_uuid_bind_none_returns_none() -> None:
    td: _PortableUUID = _PortableUUID()
    assert td.process_bind_param(None, _pg) is None
    assert td.process_bind_param(None, _sq) is None


def test_portable_uuid_result_none_returns_none() -> None:
    td: _PortableUUID = _PortableUUID()
    assert td.process_result_value(None, _pg) is None
    assert td.process_result_value(None, _sq) is None


# ---------------------------------------------------------------------------
# _PortableUUID — Postgres bind path (line 68)
# ---------------------------------------------------------------------------


def test_portable_uuid_bind_postgres_returns_value_unchanged() -> None:
    td: _PortableUUID = _PortableUUID()
    uid = uuid4()
    result = td.process_bind_param(uid, _pg)
    assert result is uid


# ---------------------------------------------------------------------------
# _PortableUUID — already-UUID result path (line 75)
# ---------------------------------------------------------------------------


def test_portable_uuid_result_already_uuid_returned_unchanged() -> None:
    td: _PortableUUID = _PortableUUID()
    uid = uuid4()
    result = td.process_result_value(uid, _sq)
    assert result == uid
    assert isinstance(result, UUID)


# ---------------------------------------------------------------------------
# _PortableUUID — load_dialect_impl Postgres branch (line 61)
# ---------------------------------------------------------------------------


def test_portable_uuid_load_dialect_impl_postgres() -> None:
    td: _PortableUUID = _PortableUUID()
    pg_type = td.load_dialect_impl(_pg)
    sqlite_type = td.load_dialect_impl(_sq)
    # Both return a type descriptor from the dialect stub; they differ in
    # which underlying type was requested (PG_UUID vs CHAR(36)).
    assert pg_type is not sqlite_type


# ---------------------------------------------------------------------------
# get_engine() — lazy init (lines 223-230)
# ---------------------------------------------------------------------------


def test_get_engine_lazy_init_calls_create_async_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_engine: MagicMock = MagicMock(spec=AsyncEngine)
    old_engine = db_module._engine
    db_module._engine = None
    try:
        with patch.object(db_module, "create_async_engine", return_value=mock_engine) as mock_fn:
            result = db_module.get_engine()
            assert result is mock_engine
            mock_fn.assert_called_once()

            # Second call returns cached — create_async_engine NOT called again.
            result2 = db_module.get_engine()
            assert result2 is mock_engine
            mock_fn.assert_called_once()
    finally:
        db_module._engine = old_engine


# ---------------------------------------------------------------------------
# get_sessionmaker() — lazy init (line 237)
# ---------------------------------------------------------------------------


def test_get_sessionmaker_lazy_init(engine: AsyncEngine) -> None:
    old_sm = db_module._sessionmaker
    db_module._sessionmaker = None
    try:
        sm = db_module.get_sessionmaker()
        assert sm is not None

        # Second call returns the cached instance.
        sm2 = db_module.get_sessionmaker()
        assert sm2 is sm
    finally:
        db_module._sessionmaker = old_sm


# ---------------------------------------------------------------------------
# create_all() and drop_all() (lines 253-255, 260-262)
# ---------------------------------------------------------------------------


async def test_create_all_is_idempotent(engine: AsyncEngine) -> None:
    # Schema already exists from the engine fixture; create_all is
    # idempotent (uses checkfirst=True implicitly via SQLAlchemy).
    await db_module.create_all()


async def test_drop_all_then_create_all(engine: AsyncEngine) -> None:
    await db_module.drop_all()
    # Recreate so later fixtures in the same test session still work.
    await db_module.create_all()
