"""Access workflow: access records, access requests and approvals.

These are the mutable parts of the access domain: they must outlive the
process, because an approval can take days (Milestone 7). Reference data
(employees, applications) still comes from data/seed; rows here are seeded
by `aegisdesk db seed`, never at application startup.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE access_requests (
            request_id         text PRIMARY KEY,
            employee_id        text NOT NULL,
            application_id     text NOT NULL,
            status             text NOT NULL CHECK (status IN
                               ('awaiting_approval', 'auto_approved', 'approved', 'rejected')),
            approvals_required text[] NOT NULL DEFAULT '{}',
            created_at         timestamptz NOT NULL,
            justification      text NOT NULL,
            thread_id          text,
            provisioned_at     timestamptz,
            idempotency_key    text UNIQUE
        )
        """
    )
    op.execute("CREATE INDEX access_requests_employee_idx ON access_requests (employee_id)")
    op.execute(
        """
        CREATE TABLE approvals (
            approval_id       text PRIMARY KEY,
            access_request_id text NOT NULL REFERENCES access_requests (request_id),
            requester_id      text NOT NULL,
            application_id    text NOT NULL,
            step              text NOT NULL CHECK (step IN ('manager', 'security', 'data_owner')),
            approver_id       text,
            approver_role     text,
            status            text NOT NULL CHECK (status IN
                              ('pending', 'approved', 'rejected', 'expired')),
            thread_id         text,
            requested_at      timestamptz NOT NULL,
            expires_at        timestamptz NOT NULL,
            decided_by        text,
            decided_at        timestamptz,
            comment           text,
            UNIQUE (access_request_id, step),
            CHECK (approver_id IS NOT NULL OR approver_role IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX approvals_pending_idx ON approvals (status) WHERE status = 'pending'")
    op.execute(
        """
        CREATE TABLE employee_access (
            employee_id    text NOT NULL,
            application_id text NOT NULL,
            role           text NOT NULL,
            granted_on     date NOT NULL,
            expires_on     date,
            -- the request that granted it (NULL for seeded/legacy access)
            access_request_id text UNIQUE REFERENCES access_requests (request_id),
            PRIMARY KEY (employee_id, application_id, granted_on)
        )
        """
    )
    op.execute("CREATE SEQUENCE access_request_seq START 1001")
    op.execute("CREATE SEQUENCE approval_seq START 1")


def downgrade() -> None:
    op.execute("DROP SEQUENCE approval_seq")
    op.execute("DROP SEQUENCE access_request_seq")
    op.execute("DROP TABLE employee_access")
    op.execute("DROP TABLE approvals")
    op.execute("DROP TABLE access_requests")
