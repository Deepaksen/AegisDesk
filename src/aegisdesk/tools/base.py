"""What a tool *is* in AegisDesk.

A tool has two sides:

* **The model-facing side:** a name, a description and a JSON Schema for its
  arguments. This is all the model ever sees. The description is effectively
  part of the prompt, because it is how the model decides when to use the
  tool.
* **The application-facing side:** the handler that runs, the output schema,
  and operational metadata (risk, read/write, idempotency, owner, timeout).
  The model never sees this, and nothing the model says can change it.

The arguments the model sends are an untrusted *request*. They are validated
against the input schema before any handler runs. Who the user is comes from
`ToolCallContext`, which the application builds, never from the arguments.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain_core.utils.json_schema import dereference_refs
from pydantic import BaseModel

from aegisdesk.identity.context import UserContext


class ToolRisk(StrEnum):
    """Risk classification from spec section 11. Enforced by policy from Milestone 6."""

    LOW = "low"  # read-only
    MEDIUM = "medium"  # reversible, low-impact write
    HIGH = "high"  # security- or business-sensitive; will require human approval


class ToolAccess(StrEnum):
    READ = "read"
    WRITE = "write"


class ToolError(Exception):
    """An expected failure the model is allowed to see and react to."""

    category = "tool_error"


class NotFoundError(ToolError):
    """The resource does not exist *or the user may not see it*.

    Both cases deliberately produce the same message, so a caller cannot probe
    for other people's records.
    """

    category = "not_found"


@dataclass(frozen=True)
class ToolCallContext:
    """Trusted, application-built context for one tool call."""

    user: UserContext
    request_id: str
    # Set by the executor for write tools; derived from the request and arguments.
    idempotency_key: str | None = None


@dataclass(frozen=True)
class ToolSpec[I: BaseModel, O: BaseModel]:
    name: str
    description: str
    input_model: type[I]
    output_model: type[O]
    handler: Callable[[I, ToolCallContext], O]

    risk: ToolRisk
    access: ToolAccess
    # True when repeating the same call cannot create a second side effect.
    idempotent: bool
    owner: str
    # Recorded now; enforced when tools move out of process (MCP, Milestone 5/11).
    timeout_seconds: float = 5.0

    def model_definition(self) -> dict[str, Any]:
        """The only part of the tool the model sees (OpenAI-style function format).

        LangChain converts this to each provider's native format (Anthropic
        `tools`, Ollama `tools`).
        """
        # Inline `$ref`s (e.g. enums) so every provider, including small local
        # models, sees a flat schema.
        parameters = dereference_refs(self.input_model.model_json_schema())
        parameters.pop("$defs", None)
        parameters.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }
