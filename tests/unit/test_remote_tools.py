"""The MCP client side: allowlist, token minting, timeouts and retry policy."""

from __future__ import annotations

import json
from typing import Any

import jwt
import mcp_types as types
import pytest

from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.identity.tokens import TOKEN_META_KEY, TokenIssuer
from aegisdesk.mcp_servers.catalogue import McpServerName
from aegisdesk.mcp_servers.server import tool_descriptor
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.access import build_access_tools
from aegisdesk.tools.executor import CompositeToolRunner, OutcomeStatus, ToolExecutor
from aegisdesk.tools.knowledge import build_knowledge_tools
from aegisdesk.tools.remote import RemoteToolRunner, ToolTransportError
from aegisdesk.tools.service_desk import build_service_desk_tools
from aegisdesk.tools.transport import ToolFactory

SECRET = "client-secret-" + "k" * 32
AGENT = AgentIdentity("service_desk", "0.1.0", "specialist", "test")


class FakeConnection:
    """Records calls; fails the first `failures` calls with `category`."""

    def __init__(
        self,
        tools: list[types.Tool],
        *,
        failures: int = 0,
        category: str = "timeout",
        result: types.CallToolResult | None = None,
    ) -> None:
        self._tools = tools
        self.failures = failures
        self.category = category
        self.result = result or types.CallToolResult(
            content=[types.TextContent(type="text", text='{"ok": true}')]
        )
        self.calls: list[dict[str, Any]] = []

    def list_tools(self, server: McpServerName) -> list[types.Tool]:
        return self._tools

    def call_tool(
        self,
        server: McpServerName,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any],
        timeout_seconds: float,
    ) -> types.CallToolResult:
        self.calls.append({"server": server, "name": name, "meta": meta})
        if len(self.calls) <= self.failures:
            raise ToolTransportError(self.category, f"simulated {self.category}")
        return self.result


@pytest.fixture
def descriptors(repository: ServiceDeskRepository) -> list[types.Tool]:
    return [tool_descriptor(t) for t in build_service_desk_tools(repository)]


def _runner(
    connection: FakeConnection, server: McpServerName, allowed: list[str]
) -> RemoteToolRunner:
    return RemoteToolRunner(
        connection,
        server,
        agent=AGENT,
        issuer=TokenIssuer(SECRET),
        allowed=allowed,
        timeout_seconds=1,
    )


def test_tool_outside_allowlist_is_refused_without_a_call(
    descriptors: list[types.Tool], aisha: UserContext
) -> None:
    connection = FakeConnection(descriptors)
    runner = _runner(connection, McpServerName.READ, ["get_my_assets"])

    outcome = runner.execute("get_ticket", {"ticket_id": "INC-1001"}, user=aisha, request_id="r")

    assert outcome.error_category == "unknown_tool"
    assert connection.calls == []
    assert runner.tool_names == ["get_my_assets"]


def test_allowlisting_a_tool_the_server_lacks_fails_at_startup(
    descriptors: list[types.Tool],
) -> None:
    with pytest.raises(ValueError, match="does not offer"):
        _runner(FakeConnection(descriptors), McpServerName.READ, ["delete_everything"])


def test_each_call_carries_a_fresh_token_for_this_server_and_agent(
    descriptors: list[types.Tool], aisha: UserContext
) -> None:
    connection = FakeConnection(descriptors)
    runner = _runner(connection, McpServerName.READ, ["get_my_assets"])

    runner.execute("get_my_assets", {}, user=aisha, request_id="req-7")
    runner.execute("get_my_assets", {}, user=aisha, request_id="req-7")

    tokens = [c["meta"][TOKEN_META_KEY] for c in connection.calls]
    assert tokens[0] != tokens[1]
    claims = jwt.decode(tokens[0], SECRET, algorithms=["HS256"], audience="aegisdesk-mcp-read")
    assert (claims["sub"], claims["rid"], claims["act"]["sub"]) == (
        "E1004",
        "req-7",
        "service_desk",
    )


def test_read_tool_is_retried_once_after_timeout(
    descriptors: list[types.Tool], aisha: UserContext
) -> None:
    connection = FakeConnection(descriptors, failures=1)
    runner = _runner(connection, McpServerName.READ, ["get_my_assets"])

    outcome = runner.execute("get_my_assets", {}, user=aisha, request_id="r")

    assert outcome.status is OutcomeStatus.OK and len(connection.calls) == 2


def test_read_tool_gives_up_after_one_retry(
    descriptors: list[types.Tool], aisha: UserContext
) -> None:
    connection = FakeConnection(descriptors, failures=5, category="unavailable")
    runner = _runner(connection, McpServerName.READ, ["get_my_assets"])

    outcome = runner.execute("get_my_assets", {}, user=aisha, request_id="r")

    assert outcome.error_category == "unavailable" and len(connection.calls) == 2
    assert json.loads(outcome.content)["error"]["message"] == "simulated unavailable"


def test_write_tool_is_never_retried(descriptors: list[types.Tool], aisha: UserContext) -> None:
    # After a timeout the ticket may or may not exist; the model is told, the
    # client does not guess.
    connection = FakeConnection(descriptors, failures=1)
    runner = _runner(connection, McpServerName.ACTION, ["create_ticket"])
    args = {
        "title": "VPN drops",
        "description": "Drops every ten minutes",
        "category": "vpn",
        "priority": "medium",
    }

    outcome = runner.execute("create_ticket", args, user=aisha, request_id="r")

    assert outcome.error_category == "timeout" and len(connection.calls) == 1


def test_unexpected_error_text_is_not_passed_to_the_model(
    descriptors: list[types.Tool], aisha: UserContext
) -> None:
    leaked = types.CallToolResult(
        content=[types.TextContent(type="text", text="Traceback: db password=hunter2")],
        is_error=True,
    )
    connection = FakeConnection(descriptors, result=leaked)
    runner = _runner(connection, McpServerName.READ, ["get_my_assets"])

    outcome = runner.execute("get_my_assets", {}, user=aisha, request_id="r")

    assert outcome.error_category == "remote_error" and "hunter2" not in outcome.content


def test_server_error_categories_are_kept(
    descriptors: list[types.Tool], aisha: UserContext
) -> None:
    body = '{"error": {"category": "not_found", "message": "No ticket INC-9"}}'
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=body)], is_error=True
    )
    connection = FakeConnection(descriptors, result=result)
    runner = _runner(connection, McpServerName.READ, ["get_ticket"])

    outcome = runner.execute("get_ticket", {"ticket_id": "INC-9"}, user=aisha, request_id="r")

    assert outcome.error_category == "not_found" and outcome.content == body


def test_remote_tools_look_identical_to_the_model(repository: ServiceDeskRepository) -> None:
    """Moving a tool behind MCP must not change what the model sees."""
    tools = [*build_access_tools(repository), *build_service_desk_tools(repository)]
    local = ToolExecutor(tools).model_definitions()
    settings = Settings(tool_transport=ToolTransport.MCP_INPROCESS)
    with ToolFactory.from_settings(settings, repository) as factory:
        remote = factory.runner(AGENT, tools).model_definitions()

    assert remote == local


def test_composite_keeps_local_tools_local(
    repository: ServiceDeskRepository, retriever: Retriever
) -> None:
    tools = [
        *build_knowledge_tools(retriever),
        *(t for t in build_service_desk_tools(repository) if t.name == "get_my_assets"),
    ]
    settings = Settings(tool_transport=ToolTransport.MCP_INPROCESS)
    with ToolFactory.from_settings(settings, repository) as factory:
        runner = factory.runner(AGENT, tools)

    assert isinstance(runner, CompositeToolRunner)
    assert runner.tool_names == [t.name for t in tools]


def test_mcp_http_requires_a_shared_secret(repository: ServiceDeskRepository) -> None:
    with pytest.raises(ValueError, match="MCP_TOKEN_SECRET"):
        ToolFactory.from_settings(Settings(tool_transport=ToolTransport.MCP_HTTP), repository)


def test_local_transport_is_the_default(repository: ServiceDeskRepository) -> None:
    factory = ToolFactory.from_settings(Settings(), repository)

    assert isinstance(factory.runner(AGENT, build_service_desk_tools(repository)), ToolExecutor)
