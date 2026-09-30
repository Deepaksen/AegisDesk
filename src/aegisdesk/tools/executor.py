"""The tool executor: the application side of tool calling.

The model only *requests* a tool call: a name and a JSON object of arguments.
The executor decides what actually happens:

1. **Lookup:** the name must be a tool this agent was given. Anything else
   (a hallucinated or forbidden tool) is refused.
2. **Validation:** arguments are parsed with the tool's Pydantic input model.
   Invalid or extra arguments are refused before any code runs.
3. **Trusted context:** the user comes from the authenticated session, and
   write tools get an idempotency key derived from the request.
4. **Execution and error shaping:** expected failures go back to the model as
   structured errors it can react to. Unexpected exceptions are logged, and
   the model gets a generic message with no stack traces or internals.
5. **Output validation:** the handler's result must match the output schema.

From Milestone 6, an `ActionGateway` (policy decision + audit events) sits
between validation and execution. A denied call, or one that needs human
approval, never reaches the handler. Every production path builds its
executor with a gateway; an executor without one is the ungoverned building
block used only in unit tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ValidationError

from aegisdesk.governance.policy import Decision
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import UserContext
from aegisdesk.tools.base import ToolAccess, ToolCallContext, ToolError, ToolSpec

if TYPE_CHECKING:
    from aegisdesk.governance.gateway import ActionGateway

logger = logging.getLogger(__name__)


class OutcomeStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class ToolOutcome:
    tool_name: str
    status: OutcomeStatus
    # JSON text returned to the model as the tool result.
    content: str
    latency_ms: float
    error_category: str | None = None


class ToolRunner(Protocol):
    """What an agent needs from its tools, wherever they run.

    `ToolExecutor` runs tools in-process (Milestones 1-4); the MCP runner in
    `tools/clients/mcp.py` runs them on MCP servers (Milestone 5). Agents and
    graphs depend only on this interface.
    """

    @property
    def tool_names(self) -> list[str]: ...

    def model_definitions(self) -> list[dict[str, Any]]: ...

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        user: UserContext,
        request_id: str,
        thread_id: str | None = None,
    ) -> ToolOutcome: ...


class CompositeToolRunner:
    """Several runners behind one interface, e.g. local knowledge tools + remote MCP tools."""

    def __init__(self, runners: Sequence[ToolRunner], *, order: Sequence[str] = ()) -> None:
        self._by_name: dict[str, ToolRunner] = {}
        for runner in runners:
            for name in runner.tool_names:
                if name in self._by_name:
                    raise ValueError(f"Duplicate tool name {name!r}")
                self._by_name[name] = runner
        rank = {name: i for i, name in enumerate(order)}
        definitions = [d for r in runners for d in r.model_definitions()]
        self._definitions = sorted(
            definitions, key=lambda d: rank.get(d["function"]["name"], len(rank))
        )

    @property
    def tool_names(self) -> list[str]:
        return [d["function"]["name"] for d in self._definitions]

    def model_definitions(self) -> list[dict[str, Any]]:
        return list(self._definitions)

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        user: UserContext,
        request_id: str,
        thread_id: str | None = None,
    ) -> ToolOutcome:
        runner = self._by_name.get(name)
        if runner is None:
            message = f"There is no tool named {name!r}."
            content = json.dumps({"error": {"category": "unknown_tool", "message": message}})
            return ToolOutcome(name, OutcomeStatus.ERROR, content, 0.0, "unknown_tool")
        return runner.execute(name, args, user=user, request_id=request_id, thread_id=thread_id)


def idempotency_key(user_id: str, request_id: str, tool_name: str, args: BaseModel) -> str:
    """Same user + same request + same tool + same validated arguments -> same key.

    A retried request, or the model repeating an identical call within one
    request, therefore cannot create a second ticket.
    """
    material = json.dumps(
        [user_id, request_id, tool_name, args.model_dump(mode="json")], sort_keys=True
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ToolExecutor:
    def __init__(
        self,
        tools: Sequence[ToolSpec[Any, Any]],
        *,
        gateway: ActionGateway | None = None,
        agent: AgentIdentity | None = None,
    ) -> None:
        # `agent` is the identity of the agent this executor serves (host side).
        # A shared executor (an MCP server) passes the caller's agent per call.
        self._gateway = gateway
        self._agent = agent
        self._tools: dict[str, ToolSpec[Any, Any]] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool name {tool.name!r}")
            self._tools[tool.name] = tool

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolSpec[Any, Any] | None:
        return self._tools.get(name)

    def model_definitions(self) -> list[dict[str, Any]]:
        return [tool.model_definition() for tool in self._tools.values()]

    @property
    def governed(self) -> bool:
        return self._gateway is not None

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        user: UserContext,
        request_id: str,
        thread_id: str | None = None,
        agent: AgentIdentity | None = None,
    ) -> ToolOutcome:
        started = time.perf_counter()

        def elapsed() -> float:
            return round((time.perf_counter() - started) * 1000, 3)

        def error(category: str, message: str) -> ToolOutcome:
            content = json.dumps({"error": {"category": category, "message": message}})
            return ToolOutcome(name, OutcomeStatus.ERROR, content, elapsed(), category)

        spec = self._tools.get(name)
        if spec is None:
            return error("unknown_tool", f"There is no tool named {name!r}.")

        try:
            parsed = spec.input_model.model_validate(args)
        except ValidationError as exc:
            return error("invalid_arguments", _describe_validation_error(exc))

        if self._gateway is None:
            return self._run(spec, parsed, user, request_id, thread_id, error, elapsed)

        authorization = self._gateway.authorize(
            tool=name,
            access=spec.access,
            args=parsed.model_dump(mode="json"),
            user=user,
            agent=agent or self._agent,
            request_id=request_id,
            thread_id=thread_id,
        )
        match authorization.decision.decision:
            case Decision.DENY:
                reasons = ", ".join(authorization.decision.reasons)
                outcome = error("policy_denied", f"This action is not allowed ({reasons}).")
            case Decision.REQUIRE_APPROVAL:
                outcome = error(
                    "approval_required",
                    "This action needs human approval before it can run. It was not performed.",
                )
            case _:
                outcome = self._run(spec, parsed, user, request_id, thread_id, error, elapsed)
        self._gateway.record_outcome(
            authorization,
            outcome=outcome.error_category or "ok",
            latency_ms=outcome.latency_ms,
        )
        return outcome

    def _run(
        self,
        spec: ToolSpec[Any, Any],
        parsed: BaseModel,
        user: UserContext,
        request_id: str,
        thread_id: str | None,
        error: Callable[[str, str], ToolOutcome],
        elapsed: Callable[[], float],
    ) -> ToolOutcome:
        name = spec.name
        key = None
        if spec.access is ToolAccess.WRITE:
            key = idempotency_key(user.employee_id, request_id, name, parsed)
        context = ToolCallContext(
            user=user, request_id=request_id, idempotency_key=key, thread_id=thread_id
        )

        try:
            output = spec.handler(parsed, context)
        except ToolError as exc:
            return error(exc.category, str(exc))
        except Exception:
            logger.exception("Tool %s failed (request_id=%s)", name, request_id)
            return error("internal_error", "The tool failed unexpectedly. Try again later.")

        if not isinstance(output, spec.output_model):
            logger.error("Tool %s returned %s, expected %s", name, type(output), spec.output_model)
            return error("invalid_output", "The tool returned an invalid result.")

        return ToolOutcome(name, OutcomeStatus.OK, output.model_dump_json(), elapsed())


def _describe_validation_error(exc: ValidationError) -> str:
    # Field locations and messages only, so the model can correct its call.
    # Input values are not echoed back.
    problems = [
        f"{'.'.join(str(p) for p in err['loc']) or '(arguments)'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    ]
    return "Invalid arguments. " + "; ".join(problems)
