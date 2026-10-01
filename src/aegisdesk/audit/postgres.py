"""Audit events in PostgreSQL (`audit_events`, created by migration 0002).

Insert and select only. The table's triggers reject UPDATE, DELETE and
TRUNCATE, so history cannot be rewritten through this or any other client
short of changing the schema (which is itself a reviewed migration).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import Text

from aegisdesk.audit.events import AuditError, AuditEvent
from aegisdesk.observability import faults

_COLUMNS = (
    "event_id, occurred_at, phase, call_id, request_id, trace_id, thread_id, user_id, "
    "agent_id, agent_version, environment, action, tool, risk, resource, policy_decision, "
    "policy_reasons, policy_version, approval_id, approver_id, outcome, latency_ms"
)

_INSERT = text(
    f"INSERT INTO audit_events ({_COLUMNS}) VALUES ("  # noqa: S608 - fixed column list
    ":event_id, :occurred_at, :phase, :call_id, :request_id, :trace_id, :thread_id, "
    ":user_id, :agent_id, :agent_version, :environment, :action, :tool, :risk, "
    "CAST(:resource AS jsonb), :policy_decision, :policy_reasons, :policy_version, "
    ":approval_id, :approver_id, :outcome, :latency_ms)"
).bindparams(bindparam("policy_reasons", type_=ARRAY(Text)))


class PgAuditLog:
    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None:
            if database_url is None:
                raise ValueError("PgAuditLog needs a database URL or an engine")
            engine = create_engine(database_url, pool_pre_ping=True)
        self._engine = engine

    def record(self, event: AuditEvent) -> None:
        if faults.active("db_error", "audit"):  # M11: the audit database is down
            raise AuditError("cannot record audit event: injected db_error")
        params: dict[str, Any] = event.model_dump(mode="python")
        params["phase"] = event.phase.value
        params["resource"] = json.dumps(event.resource)
        params["policy_reasons"] = list(event.policy_reasons)
        try:
            with self._engine.begin() as conn:
                conn.execute(_INSERT, params)
        except SQLAlchemyError as exc:
            raise AuditError(f"cannot record audit event: {type(exc).__name__}") from exc

    def query(
        self,
        *,
        request_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        sql = text(
            f"SELECT {_COLUMNS} FROM audit_events "  # noqa: S608 - fixed column list
            "WHERE (CAST(:request_id AS text) IS NULL OR request_id = :request_id) "
            "AND (CAST(:user_id AS text) IS NULL OR user_id = :user_id) "
            "ORDER BY occurred_at DESC, event_id LIMIT :limit"
        )
        params = {"request_id": request_id, "user_id": user_id, "limit": limit}
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(sql, params).mappings().all()
        except SQLAlchemyError as exc:
            raise AuditError(f"cannot read audit events: {type(exc).__name__}") from exc
        events = [
            AuditEvent.model_validate(
                {
                    **row,
                    "event_id": str(row["event_id"]),
                    "policy_reasons": tuple(row["policy_reasons"] or ()),
                }
            )
            for row in rows
        ]
        return list(reversed(events))
