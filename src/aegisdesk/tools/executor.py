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

From Milestone 6, a policy check (OPA) is added between steps 3 and 4.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from aegisdesk.identity.context import UserContext
from aegisdesk.tools.base import ToolAccess, ToolCallContext, ToolError, ToolSpec

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
    def __init__(self, tools: Sequence[ToolSpec[Any, Any]]) -> None:
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

    def execute(
        self, name: str, args: dict[str, Any], *, user: UserContext, request_id: str
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

        key = None
        if spec.access is ToolAccess.WRITE:
            key = idempotency_key(user.employee_id, request_id, name, parsed)
        context = ToolCallContext(user=user, request_id=request_id, idempotency_key=key)

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
