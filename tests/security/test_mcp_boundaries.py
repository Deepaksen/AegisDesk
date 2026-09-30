"""MCP server boundaries: the servers trust tokens, not clients or arguments.

These talk to the real servers over the MCP protocol (in-process transport),
sending raw `tools/call` requests the way a compromised or buggy client could.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import mcp_types as types
import pytest

from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.identity.tokens import TOKEN_META_KEY, TokenIssuer
from aegisdesk.mcp_servers.catalogue import (
    ACTION_TOOLS,
    READ_TOOLS,
    McpServerName,
    build_servers,
)
from aegisdesk.tools.remote import McpGateway

SECRET = "server-secret-" + "z" * 32
AGENT = AgentIdentity("service_desk", "0.1.0", "specialist", "development")
ACCESS_AGENT = AgentIdentity("access", "0.1.0", "specialist", "development")
READ, ACTION = McpServerName.READ, McpServerName.ACTION


@pytest.fixture
def mcp(repository: ServiceDeskRepository, gateway: ActionGateway) -> Iterator[McpGateway]:
    servers = build_servers(repository, SECRET, gateway=gateway)
    with McpGateway(dict(servers), timeout_seconds=5) as gw:
        yield gw


def _call(
    mcp: McpGateway,
    server: McpServerName,
    name: str,
    args: dict[str, Any],
    *,
    user: UserContext,
    audience: str | None = None,
    request_id: str = "req-1",
    secret: str = SECRET,
    agent: AgentIdentity = AGENT,
) -> tuple[bool, dict[str, Any]]:
    token = TokenIssuer(secret).issue(
        user=user, agent=agent, request_id=request_id, audience=audience or server.audience
    )
    result = mcp.call_tool(server, name, args, meta={TOKEN_META_KEY: token}, timeout_seconds=5)
    text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
    return bool(result.is_error), json.loads(text)


def test_discovery_splits_tools_by_risk(mcp: McpGateway) -> None:
    read = {t.name: t for t in mcp.list_tools(READ)}
    action = {t.name: t for t in mcp.list_tools(ACTION)}

    assert set(read) == READ_TOOLS and set(action) == ACTION_TOOLS
    assert all(t.annotations and t.annotations.read_only_hint for t in read.values())
    assert not any(t.annotations and t.annotations.read_only_hint for t in action.values())
    assert {(t.meta or {})["aegisdesk/risk"] for t in action.values()} == {"medium"}
    # No tool schema lets the caller name the user.
    for tool in [*read.values(), *action.values()]:
        assert "employee_id" not in tool.input_schema.get("properties", {})
        assert tool.input_schema.get("additionalProperties") is False


def test_call_runs_as_the_token_user(mcp: McpGateway, aisha: UserContext) -> None:
    is_error, body = _call(mcp, READ, "get_employee_profile", {}, user=aisha, agent=ACCESS_AGENT)

    assert not is_error and body["employee_id"] == "E1004"


def test_call_without_token_is_refused(mcp: McpGateway) -> None:
    result = mcp.call_tool(READ, "get_employee_profile", {}, meta={}, timeout_seconds=5)

    assert result.is_error
    assert "unauthenticated" in result.content[0].text  # type: ignore[union-attr]


def test_forged_token_is_refused(mcp: McpGateway, aisha: UserContext) -> None:
    is_error, body = _call(
        mcp, READ, "get_employee_profile", {}, user=aisha, secret="forged-" + "q" * 32
    )

    assert is_error and body["error"]["category"] == "unauthenticated"


def test_read_token_cannot_be_replayed_against_action_server(
    mcp: McpGateway, aisha: UserContext, repository: ServiceDeskRepository
) -> None:
    before = len(repository.list_tickets_for("E1004"))
    is_error, body = _call(
        mcp,
        ACTION,
        "create_ticket",
        {
            "title": "VPN drops",
            "description": "VPN drops every ten minutes",
            "category": "vpn",
            "priority": "medium",
        },
        user=aisha,
        audience=READ.audience,
    )

    assert is_error and body["error"]["category"] == "unauthenticated"
    assert len(repository.list_tickets_for("E1004")) == before


def test_read_server_does_not_offer_write_tools(mcp: McpGateway, aisha: UserContext) -> None:
    is_error, body = _call(
        mcp,
        READ,
        "create_access_request",
        {"application": "FinanceERP", "justification": "month-end reporting"},
        user=aisha,
    )

    assert is_error and body["error"]["category"] == "unknown_tool"


def test_identity_in_arguments_is_rejected(mcp: McpGateway, aisha: UserContext) -> None:
    is_error, body = _call(mcp, READ, "get_my_assets", {"employee_id": "E1010"}, user=aisha)

    assert is_error and body["error"]["category"] == "invalid_arguments"


def test_cannot_comment_on_someone_elses_ticket(
    mcp: McpGateway, repository: ServiceDeskRepository
) -> None:
    # INC-1001 belongs to E1004; E1005 tries to comment on it.
    tom = authenticate(repository, "E1005")
    is_error, body = _call(
        mcp,
        ACTION,
        "add_ticket_comment",
        {"ticket_id": "INC-1001", "comment": "Please close this"},
        user=tom,
    )

    assert is_error and body["error"]["category"] == "not_found"
    assert repository.comments_for("INC-1001") == []


def test_retried_write_with_same_request_is_idempotent(
    mcp: McpGateway, aisha: UserContext, repository: ServiceDeskRepository
) -> None:
    args = {
        "title": "VPN drops",
        "description": "VPN drops every ten minutes",
        "category": "vpn",
        "priority": "medium",
    }
    _, first = _call(mcp, ACTION, "create_ticket", args, user=aisha, request_id="r-9")
    _, second = _call(mcp, ACTION, "create_ticket", args, user=aisha, request_id="r-9")
    _, third = _call(mcp, ACTION, "create_ticket", args, user=aisha, request_id="r-10")

    ids = [r["ticket"]["ticket_id"] for r in (first, second, third)]
    assert ids[0] == ids[1] and second["created"] is False
    assert ids[2] != ids[0]
