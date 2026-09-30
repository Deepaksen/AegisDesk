"""Policy tests: the decision table for agent x tool x environment x user.

These exercise `config/policy.yaml` through the engine, with no model, tool
or gateway involved, so a policy change shows up here first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aegisdesk.agents.service_desk import POLICY_AGENT_ID
from aegisdesk.agents.supervisor import specialist_tools
from aegisdesk.config import PROJECT_ROOT
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.policy import (
    Decision,
    PolicyData,
    PolicyEngine,
    PolicyError,
    PolicyInput,
)
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.access import build_access_tools
from aegisdesk.tools.base import ToolRisk
from aegisdesk.tools.handoff import AgentName, build_handoff_tool
from aegisdesk.tools.knowledge import build_knowledge_tools
from aegisdesk.tools.service_desk import build_service_desk_tools

POLICY = PROJECT_ROOT / "config" / "policy.yaml"


@pytest.fixture(scope="module")
def engine() -> PolicyEngine:
    return PolicyEngine.from_file(POLICY)


def _agent(agent_id: str, environment: str = "development") -> AgentIdentity:
    return AgentIdentity(agent_id, "0.1.0", "specialist", environment)


def _ask(
    engine: PolicyEngine,
    agent: str | None,
    tool: str,
    user: UserContext,
    *,
    environment: str = "development",
    agent_environment: str | None = None,
) -> tuple[Decision, tuple[str, ...]]:
    identity = None if agent is None else _agent(agent, agent_environment or environment)
    decision = engine.evaluate(
        PolicyInput(tool=tool, user=user, agent=identity, environment=environment)
    )
    return decision.decision, decision.reasons


# Spec section 14, agent-to-tool authorization.
@pytest.mark.parametrize(
    ("agent", "tool", "expected"),
    [
        ("knowledge", "search_knowledge_base", Decision.ALLOW),
        ("knowledge", "create_ticket", Decision.DENY),
        ("knowledge", "create_access_request", Decision.DENY),
        ("service_desk", "create_ticket", Decision.ALLOW),
        ("service_desk", "add_ticket_comment", Decision.ALLOW),
        ("service_desk", "create_access_request", Decision.DENY),
        ("service_desk", "get_employee_profile", Decision.DENY),
        ("access", "create_access_request", Decision.ALLOW),
        ("access", "check_access_eligibility", Decision.ALLOW),
        ("access", "get_my_assets", Decision.DENY),
        ("access", "create_ticket", Decision.DENY),
        ("supervisor", "get_my_assets", Decision.DENY),
        (POLICY_AGENT_ID, "create_ticket", Decision.ALLOW),
        (POLICY_AGENT_ID, "create_access_request", Decision.DENY),
    ],
)
def test_agent_tool_matrix(
    engine: PolicyEngine, aisha: UserContext, agent: str, tool: str, expected: Decision
) -> None:
    decision, reasons = _ask(engine, agent, tool, aisha)

    assert decision is expected, reasons
    if expected is Decision.DENY:
        assert reasons  # every deny says why


def test_unknown_agent_and_missing_identity_are_denied(
    engine: PolicyEngine, aisha: UserContext
) -> None:
    assert _ask(engine, "helpful_bot", "get_my_assets", aisha)[1] == ("unknown_agent",)
    assert _ask(engine, None, "get_my_assets", aisha)[1] == ("unknown_agent",)


@pytest.mark.parametrize("tool", ["direct_grant_production_admin", "grant_access"])
def test_forbidden_actions_are_denied_for_every_agent(
    engine: PolicyEngine, aisha: UserContext, tool: str
) -> None:
    for agent in engine.data.agents:
        decision, reasons = _ask(engine, agent, tool, aisha)
        assert decision is Decision.DENY and "forbidden_action" in reasons


def test_unclassified_tool_is_denied(engine: PolicyEngine, aisha: UserContext) -> None:
    assert "unclassified_tool" in _ask(engine, "access", "export_all_employees", aisha)[1]


def test_all_deny_reasons_are_reported(engine: PolicyEngine, aisha: UserContext) -> None:
    _, reasons = _ask(
        engine,
        "knowledge",
        "create_ticket",
        aisha,
        environment="production",
        agent_environment="development",
    )

    assert set(reasons) == {
        "agent_not_authorized_for_tool",
        "environment_mismatch",
        "not_approved_for_environment",
    }


def test_production_allows_reads_but_not_writes_yet(
    engine: PolicyEngine, aisha: UserContext
) -> None:
    env = {"environment": "production"}
    assert _ask(engine, "service_desk", "get_my_assets", aisha, **env)[0] is Decision.ALLOW
    decision, reasons = _ask(engine, "service_desk", "create_ticket", aisha, **env)
    assert decision is Decision.DENY and reasons == ("not_approved_for_environment",)


def test_unknown_environment_is_denied(engine: PolicyEngine, aisha: UserContext) -> None:
    decision, reasons = _ask(engine, "service_desk", "get_my_assets", aisha, environment="qa")
    assert decision is Decision.DENY and "not_approved_for_environment" in reasons


def _custom(tmp_path: Path, **overrides: object) -> PolicyEngine:
    data = PolicyEngine.from_file(POLICY).data.model_dump(mode="json")
    data.update(overrides)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return PolicyEngine.from_file(path)


def test_high_risk_requires_approval(tmp_path: Path, aisha: UserContext) -> None:
    engine = _custom(
        tmp_path,
        tools={
            **PolicyEngine.from_file(POLICY).data.model_dump(mode="json")["tools"],
            "provision_access": {"risk": "high"},
        },
        agents={"access": ["provision_access"]},
    )

    decision, reasons = _ask(engine, "access", "provision_access", aisha)

    assert decision is Decision.REQUIRE_APPROVAL and reasons == ("high_risk_requires_approval",)


def test_medium_write_must_be_explicitly_authorized(tmp_path: Path, aisha: UserContext) -> None:
    engine = _custom(tmp_path, authorized_writes=["create_ticket"])

    assert _ask(engine, "service_desk", "create_ticket", aisha)[0] is Decision.ALLOW
    decision, reasons = _ask(engine, "service_desk", "add_ticket_comment", aisha)
    assert decision is Decision.DENY and reasons == ("write_not_authorized",)


def test_required_roles(tmp_path: Path, aisha: UserContext) -> None:
    engine = _custom(tmp_path, required_roles={"list_my_access": ["manager"]})

    decision, reasons = _ask(engine, "access", "list_my_access", aisha)
    assert decision is Decision.DENY and reasons == ("missing_role",)
    manager = UserContext("E1010", ("employee", "manager"), "finance", None)
    assert _ask(engine, "access", "list_my_access", manager)[0] is Decision.ALLOW


def test_invalid_policy_stops_startup(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text(
        "version: 1\ntools: {}\nenvironments: {}\nagents: {knowledge: [search_knowledge_base]}\n"
    )
    with pytest.raises(PolicyError, match="unclassified"):
        PolicyEngine.from_file(bad)
    with pytest.raises(PolicyError):
        PolicyEngine.from_file(tmp_path / "missing.yaml")
    bad.write_text("version: 1\ntools: {}\nagents: {}\nenvironments: {}\nsurprise: true\n")
    with pytest.raises(PolicyError, match="surprise"):
        PolicyEngine.from_file(bad)


def test_evaluation_error_fails_closed(aisha: UserContext) -> None:
    class Broken(PolicyEngine):
        def _evaluate(self, request: PolicyInput) -> object:  # type: ignore[override]
            raise RuntimeError("boom")

    engine = Broken(PolicyEngine.from_file(POLICY).data, version="x")
    decision = engine.evaluate(
        PolicyInput(tool="get_my_assets", user=aisha, agent=_agent("service_desk"), environment="d")
    )

    assert decision.decision is Decision.DENY and decision.reasons == ("policy_error",)


def test_version_changes_with_content(tmp_path: Path) -> None:
    same = PolicyEngine.from_file(POLICY).version
    assert same == PolicyEngine.from_file(POLICY).version
    assert _custom(tmp_path, authorized_writes=[]).version != same


# Consistency: the host allowlist, the policy and the tools' own metadata agree.


def test_agent_grants_match_the_host_allowlists(
    engine: PolicyEngine, repository: ServiceDeskRepository, retriever: Retriever
) -> None:
    for agent, tools in specialist_tools(repository, retriever).items():
        assert {t.name for t in tools} == engine.agent_tools(agent.value), agent
    single = {t.name for t in build_service_desk_tools(repository)}
    single |= {t.name for t in build_knowledge_tools(retriever)}
    assert single == engine.agent_tools(POLICY_AGENT_ID)


def test_declared_risk_matches_the_policy(
    engine: PolicyEngine, repository: ServiceDeskRepository, retriever: Retriever
) -> None:
    tools = [
        *build_service_desk_tools(repository),
        *build_access_tools(repository),
        *build_knowledge_tools(retriever),
        build_handoff_tool(AgentName.ACCESS),
    ]
    for tool in tools:
        assert engine.data.tools[tool.name].risk is tool.risk, tool.name


def test_shipped_policy_is_valid(engine: PolicyEngine) -> None:
    assert isinstance(engine.data, PolicyData)
    high = {name for name, r in engine.data.tools.items() if r.risk is ToolRisk.HIGH}
    assert high == {"provision_access"}


def test_only_the_approval_workflow_may_provision(engine: PolicyEngine) -> None:
    holders = {agent for agent, tools in engine.data.agents.items() if "provision_access" in tools}
    assert holders == {"access_workflow"}
