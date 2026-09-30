"""Calling tools on MCP servers: the host/client side of Milestone 5.

Roles (MCP terms):

* **host**   - our application: owns the model, the user session and the
               signing key; decides which agent may see which tools.
* **client** - one MCP `Client` connection per server, held by `McpGateway`.
* **server** - `aegisdesk-read` and `aegisdesk-action` (`aegisdesk.mcp_servers`).

`RemoteToolRunner` implements the same `ToolRunner` interface as the local
`ToolExecutor`, so agents and graphs do not know where a tool runs. Per call it:

1. refuses tools outside this agent's allowlist (before any network I/O);
2. mints a short-lived delegation token for (user, agent, request, server);
3. sends `tools/call` with the token in `_meta`, under a timeout;
4. maps transport failures to structured errors the model can react to:
   `timeout`, `unavailable`, `protocol_error`;
5. retries once only for read-only tools. Writes are never retried by the
   client: a retry after a timeout might repeat an action that did happen.
   (Writes are also idempotent by key on the server, but we do not rely on
   that to justify blind retries.)

The MCP SDK is async; agents are sync. `McpGateway` runs one event loop in a
background thread (an anyio blocking portal) and keeps the client sessions
open on it, so each tool call is a cheap request on an existing session.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Collection, Mapping
from contextlib import AbstractContextManager, ExitStack
from typing import Any, Protocol, cast

import anyio
import mcp_types as types
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp.client.client import Client
from mcp.server import Server
from mcp.shared.exceptions import MCPError

from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.identity.tokens import TOKEN_META_KEY, TokenIssuer
from aegisdesk.mcp_servers.catalogue import McpServerName
from aegisdesk.observability import faults, propagation, tracing
from aegisdesk.reliability.breaker import CircuitBreaker, breaker
from aegisdesk.tools.executor import (
    OutcomeStatus,
    ToolOutcome,
    record_tool_metrics,
    refused_unknown_tool,
)

logger = logging.getLogger(__name__)

McpTarget = Server[Any] | str  # an in-process server, or a Streamable HTTP URL


class ToolTransportError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class McpConnection(Protocol):
    """What `RemoteToolRunner` needs from a connection (a fake in unit tests)."""

    def list_tools(self, server: McpServerName) -> list[types.Tool]: ...

    def call_tool(
        self,
        server: McpServerName,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any],
        timeout_seconds: float,
    ) -> types.CallToolResult: ...


class McpGateway:
    """One MCP client session per server, shared by every agent in the process.

    Use as a context manager (or call `close()`): it owns a background thread.
    A session that fails is dropped and reopened on the next call.
    """

    def __init__(
        self, targets: Mapping[McpServerName, McpTarget], *, timeout_seconds: float
    ) -> None:
        self._targets = dict(targets)
        self._timeout = timeout_seconds
        self._stack = ExitStack()
        self._portal: BlockingPortal = self._stack.enter_context(start_blocking_portal())
        self._sessions: dict[McpServerName, tuple[Client, AbstractContextManager[Client]]] = {}

    def __enter__(self) -> McpGateway:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        for server in list(self._sessions):
            self._drop(server)
        self._stack.close()

    def _client(self, server: McpServerName) -> Client:
        if server in self._sessions:
            return self._sessions[server][0]
        target = self._targets.get(server)
        if target is None:
            raise ToolTransportError("unavailable", f"No MCP server configured for {server}.")
        manager = self._portal.wrap_async_context_manager(
            Client(target, read_timeout_seconds=self._timeout)
        )
        try:
            client = manager.__enter__()
        except Exception as exc:
            logger.warning("MCP %s: cannot connect: %s", server, exc)
            raise ToolTransportError(
                "unavailable", f"The {server} tool server is unavailable."
            ) from exc
        self._sessions[server] = (client, manager)
        return client

    def _drop(self, server: McpServerName) -> None:
        _client, manager = self._sessions.pop(server)
        try:
            manager.__exit__(None, None, None)
        except Exception:  # a broken session may fail to close cleanly
            logger.debug("MCP %s: error closing session", server, exc_info=True)

    def list_tools(self, server: McpServerName) -> list[types.Tool]:
        client = self._client(server)
        try:
            result = self._portal.call(client.list_tools)
        except Exception as exc:
            self._drop(server)
            raise _transport_error(server, exc) from exc
        return list(result.tools)

    def call_tool(
        self,
        server: McpServerName,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: dict[str, Any],
        timeout_seconds: float,
    ) -> types.CallToolResult:
        client = self._client(server)

        async def call() -> types.CallToolResult:
            # The outer deadline also covers a server that never answers.
            with anyio.fail_after(timeout_seconds):
                return await client.call_tool(
                    name,
                    arguments,
                    read_timeout_seconds=timeout_seconds,
                    meta=cast(types.RequestParamsMeta, meta),
                )

        try:
            return self._portal.call(call)
        except Exception as exc:
            error = _transport_error(server, exc)
            if error.category == "unavailable":
                self._drop(server)
            raise error from exc


def _transport_error(server: McpServerName, exc: Exception) -> ToolTransportError:
    if isinstance(exc, TimeoutError):
        return ToolTransportError("timeout", f"The {server} tool server did not answer in time.")
    if isinstance(exc, MCPError):
        if "timed out" in exc.message.lower():
            return ToolTransportError(
                "timeout", f"The {server} tool server did not answer in time."
            )
        return ToolTransportError("protocol_error", f"The {server} tool server refused the call.")
    logger.warning("MCP %s: transport failure: %s: %s", server, type(exc).__name__, exc)
    return ToolTransportError("unavailable", f"The {server} tool server is unavailable.")


def _model_definition(tool: types.Tool) -> dict[str, Any]:
    # Same shape as ToolSpec.model_definition, so the model sees identical
    # tools whether they run locally or remotely.
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


class RemoteToolRunner:
    """The tools one agent may use on one MCP server."""

    def __init__(
        self,
        connection: McpConnection,
        server: McpServerName,
        *,
        agent: AgentIdentity,
        issuer: TokenIssuer,
        allowed: Collection[str],
        timeout_seconds: float,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._connection = connection
        self._server = server
        # M11: one breaker per server, shared by every agent's runner in the process.
        self._circuit = circuit or breaker(f"mcp:{server}")
        self._agent = agent
        self._issuer = issuer
        self._timeout = timeout_seconds
        # Discovery: the server says what it offers; the host decides what
        # this agent gets. Allowlisting a tool the server does not offer is a
        # deployment mistake, so fail at startup, not mid-conversation.
        offered = {t.name: t for t in connection.list_tools(server)}
        missing = sorted(set(allowed) - set(offered))
        if missing:
            raise ValueError(f"MCP server {server} does not offer {missing}")
        self._tools = {name: offered[name] for name in offered if name in allowed}

    @property
    def server(self) -> McpServerName:
        return self._server

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    def model_definitions(self) -> list[dict[str, Any]]:
        return [_model_definition(t) for t in self._tools.values()]

    def is_read_only(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.annotations and tool.annotations.read_only_hint)

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        user: UserContext,
        request_id: str,
        thread_id: str | None = None,
    ) -> ToolOutcome:
        started = time.perf_counter()

        def outcome(status: OutcomeStatus, content: str, category: str | None) -> ToolOutcome:
            latency = round((time.perf_counter() - started) * 1000, 3)
            return ToolOutcome(name, status, content, latency, category)

        def error(category: str, message: str) -> ToolOutcome:
            body = json.dumps({"error": {"category": category, "message": message}})
            return outcome(OutcomeStatus.ERROR, body, category)

        if name not in self._tools:
            return refused_unknown_tool(name)

        if not self._circuit.allow():
            # The server kept failing: fail fast instead of waiting for another timeout.
            with tracing.span(
                f"mcp.call {self._server}/{name}",
                **{tracing.TOOL_NAME: name, "aegisdesk.mcp.server": str(self._server)},
            ) as current:
                tracing.mark_error(current, "circuit_open")
            failed = error(
                "unavailable",
                f"The {self._server} tool server is temporarily unavailable, so this was not "
                "done. Tell the user to try again in a minute.",
            )
            record_tool_metrics(failed)
            return failed

        attempts = 2 if self.is_read_only(name) else 1
        for attempt in range(1, attempts + 1):
            # A fresh token per attempt: short-lived, and bound to this server.
            token = self._issuer.issue(
                user=user,
                agent=self._agent,
                request_id=request_id,
                audience=self._server.audience,
                thread_id=thread_id,
            )
            try:
                with tracing.span(
                    f"mcp.call {self._server}/{name}",
                    **{
                        "rpc.system": "mcp",
                        "rpc.method": "tools/call",
                        tracing.TOOL_NAME: name,
                        "aegisdesk.mcp.server": str(self._server),
                        "aegisdesk.mcp.attempt": attempt,
                    },
                ) as call_span:
                    try:
                        _inject_faults(self._server, name)
                        result = self._connection.call_tool(
                            self._server,
                            name,
                            args,
                            meta=propagation.inject({TOKEN_META_KEY: token}),
                            timeout_seconds=self._timeout,
                        )
                    except ToolTransportError as exc:
                        tracing.mark_error(call_span, exc.category)
                        raise
            except ToolTransportError as exc:
                logger.warning(
                    "MCP %s: %s attempt %d/%d failed: %s (request_id=%s)",
                    self._server,
                    name,
                    attempt,
                    attempts,
                    exc.category,
                    request_id,
                )
                if attempt < attempts and exc.category in {"timeout", "unavailable"}:
                    continue
                self._circuit.record_failure()
                failed = error(exc.category, str(exc))
                # The server never ran the tool, so this is the only place to count it.
                record_tool_metrics(failed)
                return failed
            self._circuit.record_success()  # the server answered (even with a tool error)
            return _to_outcome(result, outcome)
        raise AssertionError("unreachable")  # pragma: no cover


def _inject_faults(server: McpServerName, tool: str) -> None:
    if faults.active("tool_timeout", tool):
        raise ToolTransportError("timeout", f"The {server} tool server did not answer in time.")
    if faults.active("mcp_unavailable", str(server)):
        raise ToolTransportError("unavailable", f"The {server} tool server is unavailable.")


def _to_outcome(
    result: types.CallToolResult,
    outcome: Callable[[OutcomeStatus, str, str | None], ToolOutcome],
) -> ToolOutcome:
    text = "".join(c.text for c in result.content if isinstance(c, types.TextContent))
    if not result.is_error:
        return outcome(OutcomeStatus.OK, text, None)
    try:
        category = str(json.loads(text)["error"]["category"])
    except (ValueError, KeyError, TypeError):
        # Not our server's error shape: do not pass unknown text to the model.
        category = "remote_error"
        text = json.dumps(
            {"error": {"category": category, "message": "The tool server reported an error."}}
        )
    return outcome(OutcomeStatus.ERROR, text, category)
