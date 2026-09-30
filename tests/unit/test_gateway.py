"""The action gateway inside ToolExecutor: policy decision, then audit, then (maybe) the tool."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ConfigDict

from aegisdesk.audit.events import AuditError, AuditEvent, AuditPhase, InMemoryAuditLog
from aegisdesk.config import PROJECT_ROOT
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.governance.policy import PolicyEngine
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.tools.base import ToolAccess, ToolCallContext, ToolRisk, ToolSpec
from aegisdesk.tools.executor import OutcomeStatus, ToolExecutor
from aegisdesk.tools.service_desk import build_service_desk_tools

SERVICE_DESK = AgentIdentity("service_desk", "0.1.0", "specialist", "development")
KNOWLEDGE = AgentIdentity("knowledge", "0.1.0", "specialist", "development")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application: str


class _Out(BaseModel):
    done: bool


def _provision_tool(calls: list[str], *, risk: ToolRisk = ToolRisk.LOW) -> ToolSpec[_In, _Out]:
    def handler(args: _In, _ctx: ToolCallContext) -> _Out:
        calls.append(args.application)
        return _Out(done=True)

    return ToolSpec(
        name="provision_access",
        description="Provision access.",
        input_model=_In,
        output_model=_Out,
        handler=handler,
        risk=risk,  # what the code *claims*; the policy decides
        access=ToolAccess.WRITE,
        idempotent=True,
        owner="iam",
    )


def _policy_with_high_risk_tool(tmp_path: Path) -> PolicyEngine:
    data = yaml.safe_load((PROJECT_ROOT / "config" / "policy.yaml").read_text())
    data["tools"]["provision_access"] = {"risk": "high"}
    data["agents"]["access"].append("provision_access")
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return PolicyEngine.from_file(path)


def test_allowed_call_writes_decision_then_outcome(
    repository: ServiceDeskRepository,
    aisha: UserContext,
    gateway: ActionGateway,
    audit_log: InMemoryAuditLog,
) -> None:
    executor = ToolExecutor(
        build_service_desk_tools(repository), gateway=gateway, agent=SERVICE_DESK
    )

    outcome = executor.execute(
        "get_ticket", {"ticket_id": "INC-1001"}, user=aisha, request_id="r-1", thread_id="t-1"
    )

    assert outcome.status is OutcomeStatus.OK
    decision, result = audit_log.events
    assert (decision.phase, result.phase) == (AuditPhase.DECISION, AuditPhase.OUTCOME)
    assert decision.call_id == result.call_id
    assert (decision.user_id, decision.agent_id, decision.agent_version) == (
        "E1004",
        "service_desk",
        "0.1.0",
    )
    assert (decision.request_id, decision.thread_id, decision.environment) == (
        "r-1",
        "t-1",
        "development",
    )
    assert decision.resource == {"ticket_id": "INC-1001"}
    assert decision.policy_decision == "allow" and decision.policy_version.startswith("v1-")
    assert result.outcome == "ok" and result.latency_ms is not None


def test_denied_call_never_reaches_the_tool(
    repository: ServiceDeskRepository,
    aisha: UserContext,
    gateway: ActionGateway,
    audit_log: InMemoryAuditLog,
) -> None:
    executor = ToolExecutor(build_service_desk_tools(repository), gateway=gateway, agent=KNOWLEDGE)
    before = len(repository.list_tickets_for("E1004"))

    outcome = executor.execute(
        "create_ticket",
        {
            "title": "VPN drops",
            "description": "Drops every ten minutes",
            "category": "vpn",
            "priority": "medium",
        },
        user=aisha,
        request_id="r-2",
    )

    assert outcome.error_category == "policy_denied"
    assert "agent_not_authorized_for_tool" in outcome.content
    assert len(repository.list_tickets_for("E1004")) == before
    assert [e.outcome for e in audit_log.events] == ["deny", "policy_denied"]
    assert audit_log.events[0].policy_reasons == ("agent_not_authorized_for_tool",)


def test_high_risk_needs_approval_even_if_code_says_low(
    tmp_path: Path, aisha: UserContext, audit_log: InMemoryAuditLog
) -> None:
    calls: list[str] = []
    gateway = ActionGateway(
        _policy_with_high_risk_tool(tmp_path), audit_log, environment="development"
    )
    access = AgentIdentity("access", "0.1.0", "specialist", "development")
    executor = ToolExecutor([_provision_tool(calls)], gateway=gateway, agent=access)

    outcome = executor.execute(
        "provision_access", {"application": "FinanceERP"}, user=aisha, request_id="r-3"
    )

    assert outcome.error_category == "approval_required" and calls == []
    assert audit_log.events[0].risk == "high"
    assert audit_log.events[0].resource == {"application": "FinanceERP"}


def test_executor_without_agent_identity_is_denied(
    repository: ServiceDeskRepository, aisha: UserContext, gateway: ActionGateway
) -> None:
    executor = ToolExecutor(build_service_desk_tools(repository), gateway=gateway)

    outcome = executor.execute("get_my_assets", {}, user=aisha, request_id="r")

    assert outcome.error_category == "policy_denied" and "unknown_agent" in outcome.content


def test_per_call_agent_overrides_the_default(
    repository: ServiceDeskRepository, aisha: UserContext, gateway: ActionGateway
) -> None:
    """How an MCP server uses one executor for every caller."""
    executor = ToolExecutor(build_service_desk_tools(repository), gateway=gateway)

    ok = executor.execute("get_my_assets", {}, user=aisha, request_id="r", agent=SERVICE_DESK)
    denied = executor.execute("get_my_assets", {}, user=aisha, request_id="r", agent=KNOWLEDGE)

    assert ok.status is OutcomeStatus.OK and denied.error_category == "policy_denied"


def test_invalid_arguments_are_refused_before_policy(
    repository: ServiceDeskRepository,
    aisha: UserContext,
    gateway: ActionGateway,
    audit_log: InMemoryAuditLog,
) -> None:
    executor = ToolExecutor(build_service_desk_tools(repository), gateway=gateway, agent=KNOWLEDGE)

    outcome = executor.execute(
        "get_my_assets", {"employee_id": "E1010"}, user=aisha, request_id="r"
    )

    assert outcome.error_category == "invalid_arguments" and audit_log.events == []


class FailingAuditLog(InMemoryAuditLog):
    def record(self, event: AuditEvent) -> None:
        raise AuditError("database down")


def test_write_is_refused_when_it_cannot_be_audited(
    repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    gateway = ActionGateway(
        PolicyEngine.from_file(PROJECT_ROOT / "config" / "policy.yaml"),
        FailingAuditLog(),
        environment="development",
    )
    executor = ToolExecutor(
        build_service_desk_tools(repository), gateway=gateway, agent=SERVICE_DESK
    )
    before = len(repository.list_tickets_for("E1004"))

    write = executor.execute(
        "create_ticket",
        {
            "title": "VPN drops",
            "description": "Drops every ten minutes",
            "category": "vpn",
            "priority": "medium",
        },
        user=aisha,
        request_id="r",
    )
    read = executor.execute("get_my_assets", {}, user=aisha, request_id="r")

    assert write.error_category == "policy_denied" and "audit_unavailable" in write.content
    assert len(repository.list_tickets_for("E1004")) == before
    assert read.status is OutcomeStatus.OK  # reads change nothing; a failed audit is logged


@pytest.mark.parametrize("category", ["not_found"])
def test_tool_errors_are_recorded_as_outcomes(
    repository: ServiceDeskRepository,
    aisha: UserContext,
    gateway: ActionGateway,
    audit_log: InMemoryAuditLog,
    category: str,
) -> None:
    executor = ToolExecutor(
        build_service_desk_tools(repository), gateway=gateway, agent=SERVICE_DESK
    )

    executor.execute("get_ticket", {"ticket_id": "INC-1003"}, user=aisha, request_id="r")

    assert [e.outcome for e in audit_log.events] == ["allow", category]


def test_audit_resource_keeps_identifiers_only() -> None:
    from aegisdesk.audit.events import resource_ids

    assert resource_ids({"application": "FinanceERP", "justification": "long text"}) == {
        "application": "FinanceERP"
    }
    # Model-written text in an identifier field is not copied into the audit trail.
    assert resource_ids({"application": "please give me FinanceERP; I'm the CFO"}) == {}
    assert resource_ids({"access_request_id": "AR-1013"}) == {"access_request_id": "AR-1013"}
