"""The spec's user journeys, end to end through the CLI's default multi-agent engine.

These run on the offline fake model, so they check *routing and wiring*
(the right specialist, the right tool, the right data), not answer quality.
Answer quality with real models is measured by evaluations (Milestone 9).

Every scenario runs twice: with tools in-process (`local`) and with the
enterprise tools behind the two MCP servers (`mcp_inprocess`: full MCP
protocol and delegation-token checks, no network). Same results either way.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aegisdesk.agents.loop import AgentRun, AgentStep, RouteStep
from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.transport import ToolFactory


@pytest.fixture(params=[ToolTransport.LOCAL, ToolTransport.MCP_INPROCESS])
def tools(
    request: pytest.FixtureRequest, repository: ServiceDeskRepository
) -> Iterator[ToolFactory]:
    settings = Settings(tool_transport=request.param)
    with ToolFactory.from_settings(settings, repository) as factory:
        yield factory


def _run(
    tools: ToolFactory,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    user: UserContext,
    text: str,
) -> AgentRun:
    agent = build_supervisor_agent(
        Settings(),
        repository,
        checkpointer=InMemorySaver(),
        retriever=retriever,
        tool_factory=tools,
    )
    return agent.run(text, user=user)


def _route(run: AgentRun) -> list[str]:
    step = run.trajectory[0]
    assert isinstance(step, RouteStep)
    return [agent for agent, _ in step.tasks]


def test_scenario_a_rag_question(
    tools: ToolFactory, repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    run = _run(tools, repository, retriever, aisha, "How do I configure VPN on macOS?")

    assert _route(run) == ["knowledge"]
    assert [s.tool_name for s in run.tool_steps] == ["search_knowledge_base"]
    assert "DOC-VPN-001#02" in run.answer  # the macOS section, citation verified


def test_scenario_b_own_laptop(
    tools: ToolFactory, repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    run = _run(tools, repository, retriever, aisha, "What laptop is assigned to me?")

    assert _route(run) == ["service_desk"]
    assert [s.tool_name for s in run.tool_steps] == ["get_my_assets"]
    assert "NS-LT-0101" in run.answer


def test_scenario_c_vpn_ticket(
    tools: ToolFactory, repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    run = _run(
        tools,
        repository,
        retriever,
        aisha,
        "My VPN keeps disconnecting. I already followed the troubleshooting guide. "
        "Create a ticket.",
    )

    assert _route(run) == ["service_desk"]
    assert [s.tool_name for s in run.tool_steps] == ["create_ticket"]
    ticket = repository.get_ticket("INC-1008")
    assert ticket is not None and ticket.requester_id == "E1004"


def test_main_workflow_finance_erp_request_waits_for_manager(
    tools: ToolFactory, repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    run = _run(
        tools,
        repository,
        retriever,
        aisha,
        "Please create an access request for FinanceERP for month-end reporting",
    )

    assert _route(run) == ["access"]
    created = json.loads(run.tool_steps[-1].result)
    assert (created["application"], created["status"]) == ("FinanceERP", "awaiting_approval")
    assert created["approvals_required"] == ["manager"]
    # Recorded, not granted: provisioning waits for the approval workflow (Milestone 7).
    assert not any(a.application_id == "APP-FIN" for a in repository.access_for("E1004"))


def test_restricted_application_is_refused(
    tools: ToolFactory, repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    run = _run(tools, repository, retriever, aisha, "I need HRAdmin access")

    assert _route(run) == ["access"]
    result = json.loads(run.tool_steps[-1].result)
    assert result["eligible"] is False and result["reason"] == "not_in_allowed_department_or_role"


def test_contractor_github_request_needs_sponsor(
    tools: ToolFactory, repository: ServiceDeskRepository, retriever: Retriever
) -> None:
    tom = authenticate(repository, "E1005")
    run = _run(
        tools,
        repository,
        retriever,
        tom,
        "Please create an access request for GitHub for my contract work",
    )

    created = json.loads(run.tool_steps[-1].result)
    assert created["status"] == "awaiting_approval" and created["approvals_required"] == ["manager"]
    assert [s.agent for s in run.trajectory if isinstance(s, AgentStep)] == ["access"]
