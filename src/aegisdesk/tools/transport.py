"""Where each agent's tools run: in-process, or behind the MCP servers.

Agents are still configured with the same `ToolSpec` lists as before
(`agents/supervisor.py`); that list is the agent's allowlist. The factory
turns it into a runner for the configured `TOOL_TRANSPORT`:

    local          ToolExecutor(all tools)                       (Milestones 1-4)
    mcp_inprocess  ToolExecutor(knowledge + handoff tools)
                   + RemoteToolRunner(read server, allowed read tools)
                   + RemoteToolRunner(action server, allowed action tools)
                   servers connected in-process (full protocol, no network)
    mcp_http       as above, servers reached over Streamable HTTP

With MCP, the host's copy of an enterprise tool is used only for its name:
its code runs on the server, behind the server's own token check.

Governance (Milestone 6): every executor gets the action gateway. With
`local`, the host's executor enforces policy. With MCP, the servers enforce
it for enterprise tools (the authoritative check, from the token's agent),
and the host enforces it for the tools it still runs itself.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import Any

from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.factory import build_gateway
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.tokens import TokenIssuer
from aegisdesk.mcp_servers.catalogue import SERVER_TOOLS, McpServerName, build_servers
from aegisdesk.tools.base import ToolSpec
from aegisdesk.tools.executor import CompositeToolRunner, ToolExecutor, ToolRunner
from aegisdesk.tools.remote import McpGateway, McpTarget, RemoteToolRunner


class ToolFactory:
    """Builds each agent's `ToolRunner`. Close it (or use `with`) to stop MCP sessions."""

    def __init__(
        self,
        transport: ToolTransport = ToolTransport.LOCAL,
        *,
        gateway: ActionGateway,
        targets: dict[McpServerName, McpTarget] | None = None,
        token_secret: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.transport = transport
        self.gateway = gateway
        self._timeout = timeout_seconds
        self._mcp: McpGateway | None = None
        self._issuer: TokenIssuer | None = None
        if transport is not ToolTransport.LOCAL:
            if not targets or not token_secret:
                raise ValueError(f"{transport} needs MCP targets and a token secret")
            self._issuer = TokenIssuer(token_secret)
            self._mcp = McpGateway(targets, timeout_seconds=timeout_seconds)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        repository: ServiceDeskRepository,
        *,
        gateway: ActionGateway | None = None,
    ) -> ToolFactory:
        gateway = gateway or build_gateway(settings, access_store=repository.access_store)
        transport = settings.tool_transport
        secret = settings.mcp_token_secret.get_secret_value() if settings.mcp_token_secret else None
        if transport is ToolTransport.LOCAL:
            return cls(gateway=gateway)
        if transport is ToolTransport.MCP_INPROCESS:
            # Host and servers share this process, so a throwaway key is enough.
            secret = secret or secrets.token_urlsafe(48)
            # In-process servers share the host's gateway (and so its audit log).
            targets: dict[McpServerName, McpTarget] = dict(
                build_servers(repository, secret, gateway=gateway)
            )
        else:
            if secret is None:
                raise ValueError("TOOL_TRANSPORT=mcp_http requires MCP_TOKEN_SECRET")
            targets = {
                McpServerName.READ: settings.mcp_read_url,
                McpServerName.ACTION: settings.mcp_action_url,
            }
        return cls(
            transport,
            gateway=gateway,
            targets=targets,
            token_secret=secret,
            timeout_seconds=settings.mcp_timeout_seconds,
        )

    def runner(self, agent: AgentIdentity, tools: Sequence[ToolSpec[Any, Any]]) -> ToolRunner:
        if self._mcp is None or self._issuer is None:
            return ToolExecutor(tools, gateway=self.gateway, agent=agent)
        remote_names = {name for names in SERVER_TOOLS.values() for name in names}
        runners: list[ToolRunner] = []
        local = [t for t in tools if t.name not in remote_names]
        if local:
            runners.append(ToolExecutor(local, gateway=self.gateway, agent=agent))
        for server, names in SERVER_TOOLS.items():
            allowed = [t.name for t in tools if t.name in names]
            if allowed:
                runners.append(
                    RemoteToolRunner(
                        self._mcp,
                        server,
                        agent=agent,
                        issuer=self._issuer,
                        allowed=allowed,
                        timeout_seconds=self._timeout,
                    )
                )
        # Keep the configured tool order, so the model sees the same tool list
        # whichever transport is used.
        return CompositeToolRunner(runners, order=[t.name for t in tools])

    def close(self) -> None:
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None

    def __enter__(self) -> ToolFactory:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
