"""Human approval (Milestone 7): who may decide, pause and resume, provisioning.

Runs on the offline fake model and the in-memory stores. The same flows over
PostgreSQL and across real processes are in tests/integration/.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.approvals.service import ApprovalError, ApprovalService
from aegisdesk.audit.events import InMemoryAuditLog
from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.access import AccessRequestStatus, ApprovalStatus
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.graphs.service_desk_graph import NotPausedError, ThreadedGraphAgent
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.persistence.checkpointer import sqlite_checkpointer
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.transport import ToolFactory

FINANCE = "Please create an access request for FinanceERP for month-end reporting"
PRODUCTION_DB = "Please create an access request for ProductionDB to follow up the P1 incident"


@pytest.fixture
def service(repository: ServiceDeskRepository, audit_log: InMemoryAuditLog) -> ApprovalService:
    return ApprovalService(repository.access_store, audit_log, environment="development")


@pytest.fixture
def agent(
    repository: ServiceDeskRepository, retriever: Retriever, gateway: ActionGateway
) -> Iterator[ThreadedGraphAgent]:
    with ToolFactory(gateway=gateway) as factory:
        yield build_supervisor_agent(
            Settings(),
            repository,
            checkpointer=InMemorySaver(),
            retriever=retriever,
            tool_factory=factory,
        )


def _user(repository: ServiceDeskRepository, employee_id: str) -> UserContext:
    return authenticate(repository, employee_id)


def _has(repository: ServiceDeskRepository, employee_id: str, app: str) -> bool:
    today = repository.today()
    return any(
        a.application_id == app and a.active_on(today) for a in repository.access_for(employee_id)
    )


# -- the workflow ------------------------------------------------------------------


def test_finance_erp_pauses_then_resumes_after_manager_approval(
    agent: ThreadedGraphAgent,
    service: ApprovalService,
    repository: ServiceDeskRepository,
    aisha: UserContext,
    audit_log: InMemoryAuditLog,
) -> None:
    run = agent.run(FINANCE, user=aisha, thread_id="t-fin")

    assert run.awaiting_approval
    [pending] = run.pending_approvals
    assert (pending["step"], pending["approver"]) == ("manager", "E1010")
    assert not _has(repository, "E1004", "APP-FIN")

    decision = service.decide(
        pending["approval_id"], _user(repository, "E1010"), approve=True, comment="ok"
    )
    assert decision.request.status is AccessRequestStatus.APPROVED
    assert not _has(repository, "E1004", "APP-FIN")  # approval alone grants nothing

    resumed = agent.resume("t-fin")

    assert "Access has been granted" in resumed.answer and "Grace Liu" in resumed.answer
    assert not resumed.awaiting_approval
    assert _has(repository, "E1004", "APP-FIN")
    assert agent.history("t-fin", aisha)[-1].text == resumed.answer  # in the employee's thread
    provisioning = [e for e in audit_log.events if e.tool == "provision_access"]
    assert {
        (e.agent_id, e.policy_decision, e.approval_id, e.approver_id) for e in provisioning
    } == {("access_workflow", "allow", pending["approval_id"], "E1010")}
    decisions = [e for e in audit_log.events if e.action == "approval_decision"]
    # Write-ahead (M11): recorded before the approval changed, then the outcome.
    assert [(e.phase.value, e.approver_id, e.outcome) for e in decisions] == [
        ("decision", "E1010", "approved"),
        ("outcome", "E1010", "approved"),
    ]
    assert decisions[0].call_id == decisions[1].call_id


def test_rejection_closes_the_request_without_access(
    agent: ThreadedGraphAgent,
    service: ApprovalService,
    repository: ServiceDeskRepository,
    aisha: UserContext,
) -> None:
    run = agent.run(FINANCE, user=aisha, thread_id="t-rej")
    approval_id = run.pending_approvals[0]["approval_id"]

    service.decide(
        approval_id, _user(repository, "E1010"), approve=False, comment="Use the reports instead"
    )
    resumed = agent.resume("t-rej")

    assert "not approved" in resumed.answer and "Use the reports instead" in resumed.answer
    assert not _has(repository, "E1004", "APP-FIN")
    request = repository.access_requests_for("E1004")[-1]
    assert request.status is AccessRequestStatus.REJECTED and request.provisioned_at is None


def test_two_steps_need_two_different_people(
    agent: ThreadedGraphAgent, service: ApprovalService, repository: ServiceDeskRepository
) -> None:
    lena = _user(repository, "E1006")  # IT admin; her ProductionDB access expired yesterday
    run = agent.run(PRODUCTION_DB, user=lena, thread_id="t-pdb")
    steps = {p["step"]: p for p in run.pending_approvals}
    assert steps["manager"]["approver"] == "E1013"
    assert steps["security"]["approver"] == "any security_approver"

    kenji = _user(repository, "E1013")
    service.decide(steps["manager"]["approval_id"], kenji, approve=True)
    after_first = agent.resume("t-pdb")
    assert after_first.awaiting_approval  # paused again: security has not decided
    assert not _has(repository, "E1006", "APP-PDB")

    with pytest.raises(ApprovalError) as no_role:
        service.decide(steps["security"]["approval_id"], kenji, approve=True)
    assert no_role.value.category == "not_found"  # not his step: he cannot even see it
    # Separation of duties: even holding the security role, he already decided a step.
    kenji_with_security = UserContext(
        kenji.employee_id, (*kenji.roles, "security_approver"), kenji.department, None
    )
    with pytest.raises(ApprovalError) as twice:
        service.decide(steps["security"]["approval_id"], kenji_with_security, approve=True)
    assert twice.value.category == "already_decided_another_step"

    service.decide(steps["security"]["approval_id"], _user(repository, "E1015"), approve=True)
    done = agent.resume("t-pdb")

    assert "Access has been granted" in done.answer and "expires on" in done.answer
    assert _has(repository, "E1006", "APP-PDB")
    grant = [a for a in repository.access_for("E1006") if a.application_id == "APP-PDB"][-1]
    assert grant.expires_on == repository.today() + timedelta(days=30)  # privileged: time-limited


def test_standard_application_is_provisioned_without_a_pause(
    agent: ThreadedGraphAgent, repository: ServiceDeskRepository
) -> None:
    marcus = _user(repository, "E1002")

    run = agent.run(
        "Please create an access request for Confluence for team docs", user=marcus, thread_id="t"
    )

    assert not run.awaiting_approval
    assert "standard application" in run.answer and "Access has been granted" in run.answer
    assert _has(repository, "E1002", "APP-CONF")


def test_repeated_decisions_and_resumes_grant_once(
    agent: ThreadedGraphAgent,
    service: ApprovalService,
    repository: ServiceDeskRepository,
    aisha: UserContext,
) -> None:
    run = agent.run(FINANCE, user=aisha, thread_id="t-idem")
    approval_id = run.pending_approvals[0]["approval_id"]
    grace = _user(repository, "E1010")

    first = service.decide(approval_id, grace, approve=True)
    again = service.decide(approval_id, grace, approve=True)
    agent.resume("t-idem")

    assert first.changed and not again.changed
    with pytest.raises(ApprovalError, match="already approved"):
        service.decide(approval_id, grace, approve=False)
    with pytest.raises(NotPausedError):
        agent.resume("t-idem")
    grants = [a for a in repository.access_for("E1004") if a.application_id == "APP-FIN"]
    assert len(grants) == 1


def test_resume_while_still_pending_pauses_again(
    agent: ThreadedGraphAgent, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    """A resume that is not backed by a decision in the store changes nothing."""
    agent.run(FINANCE, user=aisha, thread_id="t-early")

    run = agent.resume("t-early")

    assert run.awaiting_approval and not _has(repository, "E1004", "APP-FIN")


def test_paused_workflow_survives_a_restart(
    tmp_path: Path,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    service: ApprovalService,
    aisha: UserContext,
) -> None:
    """Everything in memory is rebuilt; only the checkpoint file and the store remain."""
    db = tmp_path / "checkpoints.sqlite"

    def fresh_agent(checkpointer: object) -> ThreadedGraphAgent:
        return build_supervisor_agent(
            Settings(),
            repository,
            checkpointer=checkpointer,  # type: ignore[arg-type]
            retriever=retriever,
            tool_factory=ToolFactory(gateway=gateway),
        )

    with sqlite_checkpointer(db) as checkpointer:
        run = fresh_agent(checkpointer).run(FINANCE, user=aisha, thread_id="t-restart")
    approval_id = run.pending_approvals[0]["approval_id"]

    service.decide(approval_id, _user(repository, "E1010"), approve=True)
    with sqlite_checkpointer(db) as checkpointer:
        resumed = fresh_agent(checkpointer).resume("t-restart")

    assert "Access has been granted" in resumed.answer
    assert _has(repository, "E1004", "APP-FIN")


# -- who may decide -----------------------------------------------------------------


@pytest.fixture
def finance_approval(repository: ServiceDeskRepository) -> str:
    app = repository.get_application("APP-FIN")
    assert app is not None
    request, _ = repository.create_access_request(
        employee_id="E1004",
        application_id="APP-FIN",
        approvals_required=list(app.approvals),
        justification="Month-end reporting",
        idempotency_key="k-fin",
    )
    return repository.access_store.approvals_for_request(request.request_id)[0].approval_id


def test_only_the_requesters_manager_may_decide(
    service: ApprovalService, repository: ServiceDeskRepository, finance_approval: str
) -> None:
    with pytest.raises(ApprovalError) as other_manager:
        service.decide(finance_approval, _user(repository, "E1011"), approve=True)
    with pytest.raises(ApprovalError) as requester:
        service.decide(finance_approval, _user(repository, "E1004"), approve=True)

    assert other_manager.value.category == "not_found"  # cannot even see it
    assert requester.value.category == "cannot_approve_own_request"
    approval = repository.access_store.get_approval(finance_approval)
    assert approval is not None and approval.status is ApprovalStatus.PENDING


def test_pending_list_shows_only_what_you_may_decide(
    service: ApprovalService, repository: ServiceDeskRepository, finance_approval: str
) -> None:
    assert [a.approval_id for a in service.list_pending_for(_user(repository, "E1010"))] == [
        finance_approval
    ]
    assert service.list_pending_for(_user(repository, "E1011")) == []
    assert service.list_pending_for(_user(repository, "E1004")) == []


def test_expired_approval_closes_the_request(
    repository: ServiceDeskRepository, audit_log: InMemoryAuditLog
) -> None:
    expired = ServiceDeskRepository.from_seed(
        Path(__file__).resolve().parents[2] / "data" / "seed",
        today=repository.today,
        approval_ttl=timedelta(seconds=-1),
    )
    request, _ = expired.create_access_request(
        employee_id="E1004",
        application_id="APP-FIN",
        approvals_required=[*expired.get_application("APP-FIN").approvals],  # type: ignore[union-attr]
        justification="Month-end reporting",
        idempotency_key="k",
    )
    service = ApprovalService(expired.access_store, audit_log, environment="development")
    [approval] = expired.access_store.approvals_for_request(request.request_id)

    with pytest.raises(ApprovalError) as exc:
        service.decide(approval.approval_id, _user(expired, "E1010"), approve=True)

    assert exc.value.category == "expired"
    assert expired.access_store.get_access_request(request.request_id).status is (  # type: ignore[union-attr]
        AccessRequestStatus.REJECTED
    )


def test_refused_decisions_are_audited(
    service: ApprovalService,
    repository: ServiceDeskRepository,
    finance_approval: str,
    audit_log: InMemoryAuditLog,
) -> None:
    with pytest.raises(ApprovalError):
        service.decide(finance_approval, _user(repository, "E1004"), approve=True)

    [event] = [e for e in audit_log.events if e.action == "approval_decision"]
    assert (event.approver_id, event.outcome) == ("E1004", "refused:cannot_approve_own_request")


def test_access_request_output_names_the_approvers(
    agent: ThreadedGraphAgent, aisha: UserContext
) -> None:
    run = agent.run(FINANCE, user=aisha, thread_id="t-msg")

    created = json.loads(run.tool_steps[-1].result)
    assert "AP-0001 (manager: E1010)" in created["next_step"]


def test_resume_provisions_through_the_mcp_action_server(
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    service: ApprovalService,
    aisha: UserContext,
    audit_log: InMemoryAuditLog,
) -> None:
    settings = Settings(tool_transport=ToolTransport.MCP_INPROCESS)
    with ToolFactory.from_settings(settings, repository, gateway=gateway) as factory:
        agent = build_supervisor_agent(
            settings,
            repository,
            checkpointer=InMemorySaver(),
            retriever=retriever,
            tool_factory=factory,
        )
        run = agent.run(FINANCE, user=aisha, thread_id="t-mcp")
        service.decide(
            run.pending_approvals[0]["approval_id"], _user(repository, "E1010"), approve=True
        )
        resumed = agent.resume("t-mcp")

    assert "Access has been granted" in resumed.answer
    assert _has(repository, "E1004", "APP-FIN")
    # Enforced on the server, from the token: the workflow identity and the thread.
    [event] = [
        e for e in audit_log.events if e.tool == "provision_access" and e.phase == "decision"
    ]
    assert (event.agent_id, event.thread_id, event.approver_id) == (
        "access_workflow",
        "t-mcp",
        "E1010",
    )
