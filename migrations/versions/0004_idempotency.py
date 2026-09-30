"""Idempotency records: replay the stored response of a duplicate request (Milestone 11).

A client that retries a message with the same Idempotency-Key gets the first
response back instead of a second run. Rows are keyed by (user_id, idem_key):
keys are the client's, so they are scoped to the caller.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE idempotency_records (
            user_id      text NOT NULL,
            idem_key     text NOT NULL,
            fingerprint  text NOT NULL,
            status       text NOT NULL CHECK (status IN ('in_progress', 'completed')),
            response     jsonb,
            created_at   timestamptz NOT NULL,
            updated_at   timestamptz NOT NULL,
            PRIMARY KEY (user_id, idem_key)
        )
        """
    )
    op.execute("CREATE INDEX idempotency_records_created_at ON idempotency_records (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE idempotency_records")
