"""SQLAlchemy ORM models for saga ledger persistence.

Tables:
- ``saga`` — one row per ILL request; lightweight pointer + current state
  (current state is a denormalised projection from the ledger; the
  ledger is still the source of truth).
- ``saga_event`` — append-only event log.
- ``inbox`` — inbound message dedup table.
- ``outbox`` — outbound delivery queue.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from agora.config import get_settings


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class _PortableUUID(TypeDecorator[UUID]):
    """UUID column that's native on Postgres and CHAR(36) on SQLite.

    Stores Python ``uuid.UUID`` objects on both sides; the SQLite
    branch handles the str <-> UUID conversion explicitly because the
    stdlib sqlite3 driver does not bind ``UUID`` instances.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, UUID):
            return value
        return UUID(str(value))


def _json_type() -> Any:
    """Use JSONB on Postgres; fall back to JSON on SQLite for tests."""
    return JSONB().with_variant(JSON(), "sqlite")


def _bigint_pk() -> Any:
    """Auto-incrementing PK that's BIGINT on Postgres but INTEGER on SQLite.

    SQLite only auto-increments columns typed as ``INTEGER PRIMARY KEY``;
    a ``BIGINT`` column will store nulls instead of generating rowids.
    """
    return BigInteger().with_variant(Integer(), "sqlite")


def _uuid_type() -> Any:
    """Portable UUID column type."""
    return _PortableUUID()


class Saga(Base):
    """Lightweight pointer for a saga; events are the source of truth."""

    __tablename__ = "saga"

    id: Mapped[UUID] = mapped_column(_uuid_type(), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(_uuid_type(), nullable=False, unique=True)
    current_state: Mapped[str] = mapped_column(String(32), nullable=False)
    iso18626_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    events: Mapped[list[SagaEventRow]] = relationship(
        back_populates="saga",
        cascade="all, delete-orphan",
        order_by="SagaEventRow.seq",
    )


class SagaEventRow(Base):
    """Append-only event row.

    Uniqueness on ``(saga_id, seq)`` enforces total ordering per saga;
    uniqueness on ``idempotency_key`` enforces replay-safety.
    """

    __tablename__ = "saga_event"
    __table_args__ = (
        UniqueConstraint("saga_id", "seq", name="uq_saga_event_seq"),
        UniqueConstraint("idempotency_key", name="uq_saga_event_idem"),
        Index("ix_saga_event_saga", "saga_id", "seq"),
    )

    id: Mapped[int] = mapped_column(_bigint_pk(), primary_key=True, autoincrement=True)
    saga_id: Mapped[UUID] = mapped_column(
        _uuid_type(), ForeignKey("saga.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    state_before: Mapped[str] = mapped_column(String(32), nullable=False)
    state_after: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    iso_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False, default=dict)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    rationale: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    saga: Mapped[Saga] = relationship(back_populates="events")


class InboxRow(Base):
    """Inbound message dedup table.

    The first time a message_id is seen, we process it and store the
    response. Repeats return the stored response without re-processing.
    """

    __tablename__ = "inbox"

    message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    response: Mapped[dict[str, Any] | None] = mapped_column(_json_type(), nullable=True)


class OutboxRow(Base):
    """Outbound delivery queue.

    The outbox worker reads pending rows and delivers to the target
    (ReShare, NCIP, etc.). External targets dedup on idempotency_key.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(_bigint_pk(), primary_key=True, autoincrement=True)
    saga_id: Mapped[UUID] = mapped_column(_uuid_type(), nullable=False)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ``claimed_at`` carries the lease for multi-worker safety. A worker
    # claiming a row flips ``status`` from ``pending`` to ``in_flight`` and
    # stamps ``claimed_at = now()``; on success/failure the row exits
    # ``in_flight`` and the column is cleared. Orphan recovery sweeps
    # ``in_flight`` rows whose ``claimed_at`` is older than the lease
    # back to ``pending``. Nullable so existing rows from before the
    # migration upgrade cleanly. See ``saga/outbox.py::outbox_claim``.
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Lazily build the async engine from settings.

    Replace via :func:`override_engine` in tests.
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.db_url,
            pool_size=settings.db_pool_size,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Lazily build the async sessionmaker."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
        )
    return _sessionmaker


def override_engine(engine: AsyncEngine) -> None:
    """Override the engine + sessionmaker (used by tests)."""
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)


async def create_all() -> None:
    """Create all tables; useful for tests against SQLite/Postgres."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    """Drop all tables; tests only."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
