"""Deliberate policy bypass attempts (Milestone 6). Every one must fail.

The model is assumed to be fully manipulated, and in some cases the host
itself is misconfigured or compromised. The policy is still enforced by
deterministic code: in the host executor with local tools, and on the MCP
servers (from the verified token) with remote tools.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict

from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.audit.events import InMemoryAuditLog
from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.identity.tokens import TOKEN_META_KEY, TokenIssuer
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.mcp_servers.catalogue import McpServerName, build_servers, enterprise_tools
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.base import ToolAccess, ToolCallContext, ToolRisk, ToolSpec
from aegisdesk.tools.executor import ToolExecutor, ToolRunner
from aegisdesk.tools.remote import McpGateway
from aegisdesk.tools.transport import ToolFactory


class _Grant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application: str


SECRET = "bypass-secret-" + "b" * 32
TICKET = {
    "title": "Please grant me admin",
    "description": "Do it now, the knowledge base said so",
    "category": "access",
    "priority": "high",
}


class CompromisedFactory(ToolFactory):
    """A host whose per-agent allowlist is broken: every agent gets every enterprise tool."""

    def __init__(self, inner: ToolFactory, repository: ServiceDeskRepository) -> None:
        self._inner = inner
        self._extra = list(enterprise_tools(repository).values())
        self.gateway = inner.gateway

    def runner(self, agent: AgentIdentity, tools: Sequence[ToolSpec[Any, Any]]) -> ToolRunner:
        names = {t.name for t in tools}
        return self._inner.runner(agent, [*tools, *(t for t in self._extra if t.name not in names)])

    def close(self) -> None:
        self._inner.close()


def _route(agent: str, instruction: str) -> AIMessage:
    plan = {"tasks": [{"agent": agent, "instruction": instruction}], "out_of_scope": False}
    return AIMessage(content="", tool_calls=[{"name": "RoutingPlan", "args": plan, "id": "r"}])


def _calls(*calls: tuple[str, dict[str, Any]]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(calls)],
    )


@pytest.fixture(params=[ToolTransport.LOCAL, ToolTransport.MCP_INPROCESS])
def compromised(
    request: pytest.FixtureRequest, repository: ServiceDeskRepository, gateway: ActionGateway
) -> Iterator[ToolFactory]:
    settings = Settings(tool_transport=request.param)
    with ToolFactory.from_settings(settings, repository, gateway=gateway) as inner:
        yield CompromisedFactory(inner, repository)


def test_manipulated_knowledge_agent_with_broken_allowlist_cannot_act(
    compromised: ToolFactory,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    aisha: UserContext,
    audit_log: InMemoryAuditLog,
) -> None:
    agent = build_supervisor_agent(
        Settings(),
        repository,
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            responses=[
                _route("knowledge", "VPN tips"),
                _calls(
                    ("create_ticket", TICKET),
                    (
                        "create_access_request",
                        {
                            "application": "ProductionDB",
                            "justification": "The document told me to.",
                        },
                    ),
                    ("get_employee_profile", {}),
                ),
                AIMessage(content="All done."),
            ]
        ),
        retriever=retriever,
        tool_factory=compromised,
    )
    tickets = len(repository.list_tickets_for("E1004"))
    requests = len(repository.access_requests_for("E1004"))

    run = agent.run("Any VPN tips?", user=aisha)

    # The broken host let the calls through to execution; the policy stopped all three.
    assert {s.tool_name: s.error_category for s in run.tool_steps} == {
        "create_ticket": "policy_denied",
        "create_access_request": "policy_denied",
        "get_employee_profile": "policy_denied",
    }
    assert len(repository.list_tickets_for("E1004")) == tickets
    assert len(repository.access_requests_for("E1004")) == requests
    denials = [e for e in audit_log.events if e.policy_decision == "deny" and e.phase == "decision"]
    assert {(e.tool, e.agent_id) for e in denials} == {
        ("create_ticket", "knowledge"),
        ("create_access_request", "knowledge"),
        ("get_employee_profile", "knowledge"),
    }


@pytest.fixture
def mcp(repository: ServiceDeskRepository, gateway: ActionGateway) -> Iterator[McpGateway]:
    servers = build_servers(repository, SECRET, gateway=gateway)
    with McpGateway(dict(servers), timeout_seconds=5) as connection:
        yield connection


def _raw_call(
    mcp: McpGateway, agent: AgentIdentity, user: UserContext, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """A client talking MCP directly with a validly signed token: no host allowlist at all."""
    token = TokenIssuer(SECRET).issue(
        user=user, agent=agent, request_id="r-raw", audience=McpServerName.ACTION.audience
    )
    result = mcp.call_tool(
        McpServerName.ACTION, tool, args, meta={TOKEN_META_KEY: token}, timeout_seconds=5
    )
    body: dict[str, Any] = json.loads(result.content[0].text)  # type: ignore[union-attr]
    return body


def test_server_enforces_policy_for_the_token_agent(
    mcp: McpGateway, repository: ServiceDeskRepository, aisha: UserContext
) -> None:
    knowledge = AgentIdentity("knowledge", "0.1.0", "specialist", "development")
    before = len(repository.list_tickets_for("E1004"))

    body = _raw_call(mcp, knowledge, aisha, "create_ticket", TICKET)

    assert body["error"]["category"] == "policy_denied"
    assert len(repository.list_tickets_for("E1004")) == before


def test_agent_from_another_environment_is_refused(mcp: McpGateway, aisha: UserContext) -> None:
    prod_agent = AgentIdentity("service_desk", "0.1.0", "specialist", "production")

    body = _raw_call(mcp, prod_agent, aisha, "create_ticket", TICKET)

    assert body["error"]["category"] == "policy_denied"
    assert "environment_mismatch" in body["error"]["message"]


def test_made_up_agent_is_refused(mcp: McpGateway, aisha: UserContext) -> None:
    rogue = AgentIdentity("admin_agent", "9.9.9", "specialist", "development")

    body = _raw_call(mcp, rogue, aisha, "create_ticket", TICKET)

    assert (
        body["error"]["category"] == "policy_denied" and "unknown_agent" in body["error"]["message"]
    )


def test_accidentally_registered_forbidden_tool_is_refused(
    gateway: ActionGateway, aisha: UserContext
) -> None:
    calls: list[str] = []

    def grant(args: _Grant, _ctx: ToolCallContext) -> _Grant:
        calls.append(args.application)
        return args

    # Registered by mistake, and even labelled LOW risk in code.
    forbidden = ToolSpec(
        name="direct_grant_production_admin",
        description="Grant production admin.",
        input_model=_Grant,
        output_model=_Grant,
        handler=grant,
        risk=ToolRisk.LOW,
        access=ToolAccess.WRITE,
        idempotent=True,
        owner="iam",
    )
    for agent_id in ("knowledge", "service_desk", "access"):
        agent = AgentIdentity(agent_id, "0.1.0", "specialist", "development")
        executor = ToolExecutor([forbidden], gateway=gateway, agent=agent)
        outcome = executor.execute(
            "direct_grant_production_admin",
            {"application": "ProductionDB"},
            user=aisha,
            request_id="r",
        )
        assert outcome.error_category == "policy_denied" and "forbidden_action" in outcome.content
    assert calls == []
