"""initial schema — saga, saga_event, inbox, outbox

Revision ID: 20260502_initial
Revises:
Create Date: 2026-05-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260502_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saga",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("current_state", sa.String(32), nullable=False),
        sa.Column("iso18626_state", sa.String(32), nullable=True),
        sa.Column("request_payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "saga_event",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "saga_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("saga.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("step", sa.String(32), nullable=False),
        sa.Column("state_before", sa.String(32), nullable=False),
        sa.Column("state_after", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("iso_message_id", sa.String(128), nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("rationale", sa.String(2048), nullable=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("saga_id", "seq", name="uq_saga_event_seq"),
        sa.UniqueConstraint("idempotency_key", name="uq_saga_event_idem"),
    )
    op.create_index("ix_saga_event_saga", "saga_event", ["saga_id", "seq"])

    op.create_table(
        "inbox",
        sa.Column("message_id", sa.String(255), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("response", postgresql.JSONB, nullable=True),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("saga_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(2048), nullable=True),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("inbox")
    op.drop_index("ix_saga_event_saga", table_name="saga_event")
    op.drop_table("saga_event")
    op.drop_table("saga")
