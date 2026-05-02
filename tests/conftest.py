"""Test fixtures.

Tests run against an in-memory SQLite database with the agora schema
created via SQLAlchemy ``create_all``. We deliberately avoid Alembic
in tests so they boot fast and offline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agora.clients.reshare import MockReShareClient, ReShareClient
from agora.saga.db import Base, override_engine


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
    )
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with sm() as s:
        yield s


@pytest.fixture
def reshare() -> ReShareClient:
    return MockReShareClient()  # type: ignore[return-value]
