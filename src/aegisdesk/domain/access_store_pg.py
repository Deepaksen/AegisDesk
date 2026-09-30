"""The access store in PostgreSQL (tables from migration 0003).

Every state change is a single conditional statement, so concurrent callers
cannot both win:

* decide:    UPDATE approvals SET ... WHERE approval_id = :id AND status = 'pending'
* status:    UPDATE access_requests SET status = :new WHERE ... AND status = :expected
* provision: UPDATE access_requests SET provisioned_at = now() WHERE ... AND provisioned_at IS NULL
             + INSERT employee_access, in the same transaction
* create:    INSERT ... ON CONFLICT (idempotency_key) DO NOTHING, then read back
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import Text

from aegisdesk.domain.access import (
    AccessRecord,
    AccessRequest,
    AccessRequestStatus,
    Approval,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalStep,
)

_REQUEST_COLUMNS = (
    "request_id, employee_id, application_id, status, approvals_required, created_at, "
    "justification, thread_id, provisioned_at"
)
_APPROVAL_COLUMNS = (
    "approval_id, access_request_id, requester_id, application_id, step, approver_id, "
    "approver_role, status, thread_id, requested_at, expires_at, decided_by, decided_at, comment"
)


def _request(row: Any) -> AccessRequest:
    return AccessRequest.model_validate(dict(row._mapping))


def _approval(row: Any) -> ApprovalRecord:
    return ApprovalRecord.model_validate(dict(row._mapping))


class PgAccessStore:
    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("PgAccessStore needs a database URL or an engine")
            engine = create_engine(database_url, pool_pre_ping=True)
        self._engine = engine

    # -- reads ----------------------------------------------------------------

    def access_for(self, employee_id: str) -> list[AccessRecord]:
        sql = text(
            "SELECT employee_id, application_id, role, granted_on, expires_on "
            "FROM employee_access WHERE employee_id = :e ORDER BY granted_on, application_id"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"e": employee_id}).all()
        return [AccessRecord.model_validate(dict(r._mapping)) for r in rows]

    def access_requests_for(self, employee_id: str) -> list[AccessRequest]:
        sql = text(
            f"SELECT {_REQUEST_COLUMNS} FROM access_requests "  # noqa: S608 - fixed columns
            "WHERE employee_id = :e ORDER BY request_id"
        )
        with self._engine.connect() as conn:
            return [_request(r) for r in conn.execute(sql, {"e": employee_id})]

    def get_access_request(self, request_id: str) -> AccessRequest | None:
        with self._engine.connect() as conn:
            return self._get_request(conn, request_id)

    def _get_request(self, conn: Connection, request_id: str) -> AccessRequest | None:
        sql = text(
            f"SELECT {_REQUEST_COLUMNS} FROM access_requests WHERE request_id = :r"  # noqa: S608
        )
        row = conn.execute(sql, {"r": request_id}).first()
        return _request(row) if row else None

    def access_request_for_key(self, idempotency_key: str) -> AccessRequest | None:
        sql = text(
            f"SELECT {_REQUEST_COLUMNS} FROM access_requests "  # noqa: S608 - fixed columns
            "WHERE idempotency_key = :k"
        )
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"k": idempotency_key}).first()
        return _request(row) if row else None

    def approvals_for_request(self, request_id: str) -> list[ApprovalRecord]:
        sql = text(
            f"SELECT {_APPROVAL_COLUMNS} FROM approvals "  # noqa: S608 - fixed columns
            "WHERE access_request_id = :r ORDER BY approval_id"
        )
        with self._engine.connect() as conn:
            return [_approval(r) for r in conn.execute(sql, {"r": request_id})]

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        sql = text(
            f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE approval_id = :a"  # noqa: S608
        )
        with self._engine.connect() as conn:
            row = conn.execute(sql, {"a": approval_id}).first()
        return _approval(row) if row else None

    def pending_approvals(self) -> list[ApprovalRecord]:
        sql = text(
            f"SELECT {_APPROVAL_COLUMNS} FROM approvals "  # noqa: S608 - fixed columns
            "WHERE status = 'pending' ORDER BY approval_id"
        )
        with self._engine.connect() as conn:
            return [_approval(r) for r in conn.execute(sql)]

    # -- writes ---------------------------------------------------------------

    def create_access_request(
        self,
        *,
        employee_id: str,
        application_id: str,
        approvals_required: list[Approval],
        steps: list[ApprovalStep],
        justification: str,
        idempotency_key: str,
        thread_id: str | None,
        approval_ttl: timedelta,
    ) -> tuple[AccessRequest, bool]:
        now = datetime.now(UTC)
        status = (
            AccessRequestStatus.AWAITING_APPROVAL
            if approvals_required
            else AccessRequestStatus.AUTO_APPROVED
        )
        insert = text(
            "INSERT INTO access_requests (request_id, employee_id, application_id, status, "
            "approvals_required, created_at, justification, thread_id, idempotency_key) "
            "VALUES ('AR-' || nextval('access_request_seq'), :employee_id, :application_id, "
            ":status, :approvals, :now, :justification, :thread_id, :key) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING request_id"
        ).bindparams(bindparam("approvals", type_=ARRAY(Text)))
        with self._engine.begin() as conn:
            created_id = conn.execute(
                insert,
                {
                    "employee_id": employee_id,
                    "application_id": application_id,
                    "status": status.value,
                    "approvals": [a.value for a in approvals_required],
                    "now": now,
                    "justification": justification,
                    "thread_id": thread_id,
                    "key": idempotency_key,
                },
            ).scalar()
            if created_id is None:
                existing: str = conn.execute(
                    text("SELECT request_id FROM access_requests WHERE idempotency_key = :k"),
                    {"k": idempotency_key},
                ).scalar_one()
                request = self._get_request(conn, existing)
                if request is None:  # pragma: no cover - the row was just found
                    raise RuntimeError(f"access request {existing} vanished")
                return request, False
            for step in steps:
                conn.execute(
                    text(
                        "INSERT INTO approvals (approval_id, access_request_id, requester_id, "
                        "application_id, step, approver_id, approver_role, status, thread_id, "
                        "requested_at, expires_at) VALUES "
                        "('AP-' || lpad(nextval('approval_seq')::text, 4, '0'), :r, :e, :app, "
                        ":step, :approver_id, :approver_role, 'pending', :thread_id, :now, :exp)"
                    ),
                    {
                        "r": created_id,
                        "e": employee_id,
                        "app": application_id,
                        "step": step.step.value,
                        "approver_id": step.approver_id,
                        "approver_role": step.approver_role,
                        "thread_id": thread_id,
                        "now": now,
                        "exp": now + approval_ttl,
                    },
                )
            request = self._get_request(conn, created_id)
        if request is None:  # pragma: no cover - inserted above
            raise RuntimeError(f"access request {created_id} vanished")
        return request, True

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: ApprovalStatus,
        decided_by: str | None,
        comment: str | None,
    ) -> bool:
        sql = text(
            "UPDATE approvals SET status = :s, decided_by = :by, decided_at = now(), "
            "comment = :c WHERE approval_id = :a AND status = 'pending'"
        )
        with self._engine.begin() as conn:
            result = conn.execute(
                sql, {"s": status.value, "by": decided_by, "c": comment, "a": approval_id}
            )
        return result.rowcount == 1

    def set_request_status(
        self, request_id: str, *, status: AccessRequestStatus, expected: AccessRequestStatus
    ) -> bool:
        sql = text(
            "UPDATE access_requests SET status = :s WHERE request_id = :r AND status = :expected"
        )
        with self._engine.begin() as conn:
            result = conn.execute(
                sql, {"s": status.value, "r": request_id, "expected": expected.value}
            )
        return result.rowcount == 1

    def provision(self, request_id: str, record: AccessRecord) -> bool:
        with self._engine.begin() as conn:
            claimed = conn.execute(
                text(
                    "UPDATE access_requests SET provisioned_at = now() "
                    "WHERE request_id = :r AND provisioned_at IS NULL"
                ),
                {"r": request_id},
            ).rowcount
            if claimed != 1:
                return False
            conn.execute(
                text(
                    "INSERT INTO employee_access (employee_id, application_id, role, granted_on, "
                    "expires_on, access_request_id) VALUES (:e, :a, :role, :on, :exp, :r)"
                ),
                {
                    "e": record.employee_id,
                    "a": record.application_id,
                    "role": record.role,
                    "on": record.granted_on,
                    "exp": record.expires_on,
                    "r": request_id,
                },
            )
        return True

    # -- seeding (aegisdesk db seed) ------------------------------------------

    def seed(self, access: list[AccessRecord], requests: list[AccessRequest]) -> int:
        """Insert seed rows that are missing; never overwrite. Returns rows inserted."""
        inserted = 0
        with self._engine.begin() as conn:
            for r in requests:
                inserted += conn.execute(
                    text(
                        "INSERT INTO access_requests (request_id, employee_id, application_id, "
                        "status, approvals_required, created_at, justification) VALUES "
                        "(:r, :e, :a, :s, :ap, :c, :j) ON CONFLICT (request_id) DO NOTHING"
                    ).bindparams(bindparam("ap", type_=ARRAY(Text))),
                    {
                        "r": r.request_id,
                        "e": r.employee_id,
                        "a": r.application_id,
                        "s": r.status.value,
                        "ap": [a.value for a in r.approvals_required],
                        "c": r.created_at,
                        "j": r.justification,
                    },
                ).rowcount
            for a in access:
                inserted += conn.execute(
                    text(
                        "INSERT INTO employee_access (employee_id, application_id, role, "
                        "granted_on, expires_on) VALUES (:e, :a, :role, :on, :exp) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "e": a.employee_id,
                        "a": a.application_id,
                        "role": a.role,
                        "on": a.granted_on,
                        "exp": a.expires_on,
                    },
                ).rowcount
            # New request IDs continue after the seeded ones.
            conn.execute(
                text(
                    "SELECT setval('access_request_seq', GREATEST((SELECT COALESCE(MAX("
                    "CAST(substring(request_id FROM 4) AS integer)), 1000) FROM access_requests), "
                    "(SELECT last_value FROM access_request_seq)))"
                )
            )
        return inserted
