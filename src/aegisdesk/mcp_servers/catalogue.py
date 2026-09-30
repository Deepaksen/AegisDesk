"""Which tools live on which MCP server.

Two servers, two trust boundaries (spec section 12):

* **read**   - LOW-risk lookups. Safe to retry. Audience `aegisdesk-mcp-read`.
* **action** - writes (MEDIUM). Never blindly retried; idempotent by key.
               Audience `aegisdesk-mcp-action`.

A token minted for one server is refused by the other, so an agent holding
read access cannot replay it against the action server.

Deliberately *not* behind MCP: the knowledge-base tools (the agents' own
retrieval capability, kept local to compare local vs remote tools) and
`request_handoff` (a control signal for the supervisor, not an integration).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from mcp.server import Server

from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.tokens import TokenVerifier
from aegisdesk.mcp_servers.server import build_tool_server
from aegisdesk.tools.access import build_access_tools
from aegisdesk.tools.base import ToolSpec
from aegisdesk.tools.service_desk import build_service_desk_tools


class McpServerName(StrEnum):
    READ = "read"
    ACTION = "action"

    @property
    def audience(self) -> str:
        return f"aegisdesk-mcp-{self.value}"


READ_TOOLS = frozenset(
    {
        "get_employee_profile",
        "get_my_assets",
        "list_my_access",
        "get_application",
        "check_access_eligibility",
        "get_ticket",
        "list_my_tickets",
    }
)
ACTION_TOOLS = frozenset({"create_ticket", "add_ticket_comment", "create_access_request"})
SERVER_TOOLS = {McpServerName.READ: READ_TOOLS, McpServerName.ACTION: ACTION_TOOLS}


def enterprise_tools(repository: ServiceDeskRepository) -> dict[str, ToolSpec[Any, Any]]:
    tools = [*build_service_desk_tools(repository), *build_access_tools(repository)]
    return {t.name: t for t in tools}


def build_servers(
    repository: ServiceDeskRepository, secret: str
) -> dict[McpServerName, Server[Any]]:
    tools = enterprise_tools(repository)
    return {
        server: build_tool_server(
            f"aegisdesk-{server.value}",
            [tools[name] for name in sorted(names)],
            TokenVerifier(secret, audience=server.audience),
        )
        for server, names in SERVER_TOOLS.items()
    }
