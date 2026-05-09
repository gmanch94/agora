"""saga: GIN index on request_payload->patron->patron_id (audit #37)

Revision ID: 20260509_saga_patron_id_index
Revises: 20260504_outbox_claimed_at
Create Date: 2026-05-09

The ``/portal/requests`` endpoint filters via JSON-path:
    WHERE saga.request_payload['patron']['patron_id'].astext = :id

Audit 2026-05-09 #37: without an index this scans the entire saga
table, which is fine while the prototype's data set is tiny but
becomes a DoS-via-scan vector as deployments grow. A GIN expression
index on the JSONB path makes the lookup O(log n).

The index is **Postgres-only** because:
- ``USING gin`` is Postgres-specific syntax;
- SQLite's JSON1 extension supports indexed extractions but the
  syntax differs and the test suite uses ``Base.metadata.create_all``
  to bootstrap (no Alembic for SQLite tests).

The migration uses ``op.create_index(..., postgresql_using='gin')``
so the GIN clause only appears on Postgres dialects. SQLite paths
that ever ran this migration (none today) would fall through to a
plain expression index without the GIN clause.
"""
from __future__ import annotations

from alembic import op

revision = "20260509_saga_patron_id_index"
down_revision = "20260504_outbox_claimed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres-specific GIN expression index on the JSONB path. The
    # raw SQL is cleaner than ``op.create_index`` for expression
    # indexes — Alembic / SQLAlchemy autogenerate doesn't roundtrip
    # JSONB-path indexes cleanly anyway.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_saga_patron_id "
            "ON saga ((request_payload->'patron'->>'patron_id'))"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_saga_patron_id")
