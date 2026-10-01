"""Expose AegisDesk tools as an MCP server.

An MCP server here is a thin, strict wrapper around the same `ToolExecutor`
the agents used locally in Milestones 1-4:

    tools/list  → the tools' JSON Schemas (discovery), plus standard MCP
                  annotations (readOnlyHint, idempotentHint, ...) and our risk class
    tools/call  → 1. verify the delegation token from `_meta` (who, which agent,
                     which request; audience = this server)
                  2. ToolExecutor.execute(name, args, user=token user,
                                          agent=token agent, request_id=token request id)
                     (schema validation, then the action gateway: policy decision
                     and audit events (Milestone 6), then idempotency, error shaping)
                  3. return the JSON result; errors as isError results

The server never takes identity from the arguments or from the client's say-so:
only a token signed with the host's key counts. The token is not echoed
anywhere.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import mcp_types as types
from mcp.server import Server

from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.identity.tokens import TOKEN_META_KEY, TokenError, TokenVerifier
from aegisdesk.observability import propagation, tracing
from aegisdesk.observability.redaction import pseudonym
from aegisdesk.tools.base import ToolAccess, ToolSpec
from aegisdesk.tools.executor import OutcomeStatus, ToolExecutor

logger = logging.getLogger(__name__)

RISK_META_KEY = "aegisdesk/risk"
ACCESS_META_KEY = "aegisdesk/access"
OWNER_META_KEY = "aegisdesk/owner"


def tool_descriptor(spec: ToolSpec[Any, Any]) -> types.Tool:
    function = spec.model_definition()["function"]
    return types.Tool(
        name=spec.name,
        description=spec.description,
        input_schema=function["parameters"],
        annotations=types.ToolAnnotations(
            read_only_hint=spec.access is ToolAccess.READ,
            destructive_hint=False,  # no tool deletes or overwrites anything
            idempotent_hint=spec.idempotent,
            open_world_hint=False,
        ),
        meta={
            RISK_META_KEY: spec.risk.value,
            ACCESS_META_KEY: spec.access.value,
            OWNER_META_KEY: spec.owner,
        },
    )


def _error(category: str, message: str) -> types.CallToolResult:
    body = json.dumps({"error": {"category": category, "message": message}})
    return types.CallToolResult(content=[types.TextContent(type="text", text=body)], is_error=True)


def build_tool_server(
    name: str,
    tools: Sequence[ToolSpec[Any, Any]],
    verifier: TokenVerifier,
    gateway: ActionGateway,
) -> Server[Any]:
    # The server is the authoritative enforcement point: whatever the host
    # allowed, the policy is checked here against the token's agent and user.
    executor = ToolExecutor(tools, gateway=gateway)
    descriptors = [tool_descriptor(t) for t in tools]

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=descriptors)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        meta = params.meta or {}
        token = meta.get(TOKEN_META_KEY)
        if not isinstance(token, str):
            token = None
        try:
            caller = verifier.verify(token)
        except TokenError as exc:
            logger.warning("mcp %s: rejected call to %s: %s", name, params.name, exc)
            return _error("unauthenticated", str(exc))

        # Continue the client's trace (traceparent in _meta): one trace across processes.
        with tracing.remote_child_span(
            propagation.extract(meta),
            f"mcp.server {params.name}",
            **{
                "rpc.system": "mcp",
                "rpc.method": "tools/call",
                tracing.TOOL_NAME: params.name,
                "aegisdesk.mcp.server": name,
                tracing.REQUEST_ID: caller.request_id,
            },
        ):
            outcome = executor.execute(
                params.name,
                dict(params.arguments or {}),
                user=caller.user,
                request_id=caller.request_id,
                thread_id=caller.thread_id,
                agent=caller.agent,
            )
        # Who did what, on whose behalf, independent of the conversation.
        logger.info(
            "mcp %s: tool=%s user=%s agent=%s@%s env=%s request_id=%s status=%s%s",
            name,
            params.name,
            pseudonym(caller.user.employee_id),  # the audit trail has the real ID
            caller.agent.agent_id,
            caller.agent.agent_version,
            caller.agent.environment,
            caller.request_id,
            outcome.status.value,
            f" error={outcome.error_category}" if outcome.error_category else "",
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=outcome.content)],
            is_error=outcome.status is OutcomeStatus.ERROR,
        )

    return Server(name, on_list_tools=list_tools, on_call_tool=call_tool)
