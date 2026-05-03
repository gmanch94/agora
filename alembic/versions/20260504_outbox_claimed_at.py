"""outbox.claimed_at — multi-worker claim lease (backlog #5)

Revision ID: 20260504_outbox_claimed_at
Revises: 20260503_approving_marker
Create Date: 2026-05-04

Adds a nullable ``claimed_at`` column to ``outbox`` so the worker can
implement a SELECT ... FOR UPDATE SKIP LOCKED claim pattern. A worker
flips ``status`` from ``pending`` to ``in_flight`` and stamps
``claimed_at = now()``; on success/failure the row exits ``in_flight``
and the column is cleared. Orphan recovery sweeps stale ``in_flight``
rows back to ``pending`` when ``claimed_at < now - lease_secs``.

Existing rows upgrade cleanly because the column is nullable and the
default ``status`` (``pending``) means no row enters ``in_flight``
until a worker claims it. No backfill needed.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260504_outbox_claimed_at"
down_revision = "20260503_approving_marker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox", "claimed_at")
