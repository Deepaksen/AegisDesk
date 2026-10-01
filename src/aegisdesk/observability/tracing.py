"""Span helpers and attribute names.

Attribute names follow the OpenTelemetry GenAI semantic conventions where
they exist (`gen_ai.*`), and `aegisdesk.*` otherwise. Values are identifiers,
names, numbers and decisions only: never message text, tool arguments or
results (see `redaction.py`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, Status, StatusCode

from aegisdesk.observability.setup import tracer_provider

# gen_ai semantic conventions
OPERATION = "gen_ai.operation.name"
SYSTEM = "gen_ai.system"
MODEL = "gen_ai.request.model"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
AGENT_NAME = "gen_ai.agent.name"
TOOL_NAME = "gen_ai.tool.name"
# aegisdesk
REQUEST_ID = "aegisdesk.request_id"
THREAD_ID = "aegisdesk.thread_id"
USER = "aegisdesk.user.hash"
AGENT_VERSION = "aegisdesk.agent.version"
PROMPT = "aegisdesk.prompt"
STATUS = "aegisdesk.status"
ERROR_CATEGORY = "aegisdesk.error.category"
GRAPH_STEPS = "aegisdesk.graph.steps"


def tracer() -> trace.Tracer:
    return tracer_provider().get_tracer("aegisdesk")


def span(name: str, **attributes: Any) -> AbstractContextManager[Span]:
    """A span with attributes; exceptions mark it as an error and propagate."""
    return remote_child_span(None, name, **attributes)


@contextmanager
def remote_child_span(parent: Context | None, name: str, **attributes: Any) -> Iterator[Span]:
    """A span continuing a trace from another process (MCP server side), or a normal one."""
    attrs = {k: v for k, v in attributes.items() if v is not None}
    with tracer().start_as_current_span(
        name,
        context=parent,
        attributes=attrs,
        record_exception=True,
        set_status_on_exception=True,
    ) as current:
        yield current


def mark_error(current: Span, category: str) -> None:
    """A handled failure: the span is an error with a category, no message text."""
    current.set_attribute(ERROR_CATEGORY, category)
    current.set_status(Status(StatusCode.ERROR, category))


def current_span_attribute(key: str, value: Any) -> None:
    """Annotate whatever span is current (a no-op outside spans)."""
    trace.get_current_span().set_attribute(key, value)


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


_PROVIDERS = {"ChatAnthropic": "anthropic", "ChatOllama": "ollama", "ScriptedChatModel": "fake"}


def model_attributes(model: object) -> dict[str, Any]:
    """gen_ai.system / gen_ai.request.model for a LangChain chat model (bound or not)."""
    bound = getattr(model, "bound", model)  # RunnableBinding from bind_tools
    name = getattr(bound, "model_name", None) or getattr(bound, "model", None)
    return {
        SYSTEM: _PROVIDERS.get(type(bound).__name__, type(bound).__name__),
        MODEL: str(name) if name else None,
    }


def record_llm_call(
    current: Span,
    *,
    model: dict[str, Any],
    input_tokens: int,
    output_tokens: int,
    seconds: float,
    agent: str,
) -> None:
    from aegisdesk.observability.metrics import instruments

    current.set_attribute(INPUT_TOKENS, input_tokens)
    current.set_attribute(OUTPUT_TOKENS, output_tokens)
    labels = {"model": str(model.get(MODEL) or "unknown"), "agent": agent}
    m = instruments()
    m.llm_calls.add(1, labels)
    m.llm_latency.record(seconds, labels)
    m.input_tokens.add(input_tokens, labels)
    m.output_tokens.add(output_tokens, labels)
