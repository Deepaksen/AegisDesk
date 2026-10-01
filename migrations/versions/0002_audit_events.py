"""Audit events: append-only (UPDATE, DELETE and TRUNCATE are rejected).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_events (
            event_id        uuid PRIMARY KEY,
            occurred_at     timestamptz NOT NULL,
            phase           text NOT NULL CHECK (phase IN ('decision', 'outcome')),
            call_id         text NOT NULL,
            request_id      text NOT NULL,
            trace_id        text,
            thread_id       text,
            user_id         text NOT NULL,
            agent_id        text,
            agent_version   text,
            environment     text NOT NULL,
            action          text NOT NULL,
            tool            text NOT NULL,
            risk            text,
            resource        jsonb NOT NULL DEFAULT '{}',
            policy_decision text NOT NULL,
            policy_reasons  text[] NOT NULL DEFAULT '{}',
            policy_version  text NOT NULL,
            approval_id     text,
            approver_id     text,
            outcome         text NOT NULL,
            latency_ms      double precision
        )
        """
    )
    op.execute("CREATE INDEX audit_events_request_idx ON audit_events (request_id)")
    op.execute("CREATE INDEX audit_events_user_time_idx ON audit_events (user_id, occurred_at)")
    op.execute(
        """
        CREATE FUNCTION audit_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only (% rejected)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_update_delete
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_truncate
            BEFORE TRUNCATE ON audit_events
            FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only()
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE audit_events")
    op.execute("DROP FUNCTION audit_events_append_only()")
