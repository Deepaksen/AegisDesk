"""Audit events in PostgreSQL: they round-trip, and history cannot be rewritten.

Needs AEGIS_TEST_DATABASE_URL pointing at a migrated database (CI provides
one). Every test uses its own request ID, so no cleanup is needed (and none
is possible: the table rejects DELETE and TRUNCATE).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from aegisdesk.audit.events import AuditEvent, AuditPhase
from aegisdesk.audit.postgres import PgAuditLog
from aegisdesk.config import PROJECT_ROOT
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.governance.policy import PolicyEngine
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.tools.executor import ToolExecutor
from aegisdesk.tools.service_desk import build_service_desk_tools

TEST_DB_URL = os.environ.get("AEGIS_TEST_DATABASE_URL")


@pytest.fixture
def engine() -> Iterator[Engine]:
    if not TEST_DB_URL:
        pytest.skip("AEGIS_TEST_DATABASE_URL not set")
    engine = create_engine(TEST_DB_URL)
    yield engine
    engine.dispose()


def _event(request_id: str, **kwargs: object) -> AuditEvent:
    fields: dict[str, object] = {
        "phase": AuditPhase.DECISION,
        "call_id": "c-1",
        "request_id": request_id,
        "user_id": "E1004",
        "agent_id": "access",
        "agent_version": "0.1.0",
        "environment": "test",
        "tool": "create_access_request",
        "risk": "medium",
        "resource": {"application": "FinanceERP"},
        "policy_decision": "deny",
        "policy_reasons": ("agent_not_authorized_for_tool", "environment_mismatch"),
        "policy_version": "v1-test",
        "outcome": "deny",
    }
    fields.update(kwargs)
    return AuditEvent.model_validate(fields)


def test_events_round_trip(engine: Engine) -> None:
    log = PgAuditLog(engine=engine)
    request_id = f"test-{uuid.uuid4()}"
    first = _event(request_id)
    second = _event(request_id, phase=AuditPhase.OUTCOME, outcome="policy_denied", latency_ms=1.5)

    log.record(first)
    log.record(second)

    stored = log.query(request_id=request_id)
    assert [e.event_id for e in stored] == [first.event_id, second.event_id]
    assert stored[0] == first
    assert stored[1].latency_ms == 1.5


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET outcome = 'ok' WHERE request_id = :rid",
        "DELETE FROM audit_events WHERE request_id = :rid",
        "TRUNCATE audit_events",
    ],
)
def test_history_cannot_be_rewritten(engine: Engine, statement: str) -> None:
    request_id = f"test-{uuid.uuid4()}"
    PgAuditLog(engine=engine).record(_event(request_id))

    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text(statement), {"rid": request_id})

    assert PgAuditLog(engine=engine).query(request_id=request_id)[0].outcome == "deny"


def test_gateway_writes_to_postgres(
    engine: Engine, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    gateway = ActionGateway(
        PolicyEngine.from_file(PROJECT_ROOT / "config" / "policy.yaml"),
        PgAuditLog(engine=engine),
        environment="development",
    )
    agent = AgentIdentity("service_desk", "0.1.0", "specialist", "development")
    executor = ToolExecutor(build_service_desk_tools(repository), gateway=gateway, agent=agent)
    request_id = f"test-{uuid.uuid4()}"

    executor.execute("get_ticket", {"ticket_id": "INC-1001"}, user=aisha, request_id=request_id)

    events = PgAuditLog(engine=engine).query(request_id=request_id)
    assert [(e.phase, e.outcome) for e in events] == [
        (AuditPhase.DECISION, "allow"),
        (AuditPhase.OUTCOME, "ok"),
    ]
    assert events[0].resource == {"ticket_id": "INC-1001"}
