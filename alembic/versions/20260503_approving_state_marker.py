"""marker: LifecycleState.APPROVING enum value

Revision ID: 20260503_approving_marker
Revises: 20260502_initial
Create Date: 2026-05-03

Per ADR-0012 §6: lifecycle state is stored as ``VARCHAR(32)`` (no
enum domain), so adding ``LifecycleState.APPROVING`` requires no DDL.
This revision is a no-op marker so the schema-version column reflects
the lifecycle change and downstream consumers have a clear signal for
when the new state appeared.
"""
from __future__ import annotations

revision = "20260503_approving_marker"
down_revision = "20260502_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op — lifecycle state is VARCHAR; new enum value needs no DDL."""


def downgrade() -> None:
    """No-op — no DDL was applied on upgrade."""
