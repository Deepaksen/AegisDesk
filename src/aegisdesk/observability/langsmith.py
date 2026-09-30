"""Optional LangSmith tracing, with content redacted before it leaves the process.

LangSmith gives LLM-native traces (every model call, prompt, tool call) and
evaluation tooling. It is off unless `LANGSMITH_TRACING=true` and an API key
are set. When on, we add a `LangChainTracer` whose client hides inputs and
outputs through `redact_payload`: identifiers and metadata stay, message and
tool text become `[REDACTED]`. Run metadata (agent, versions, prompt, request
and thread IDs) is attached through the RunnableConfig.
"""

from __future__ import annotations

import os
from typing import Any

from aegisdesk.observability.redaction import REDACTED, scrub

# Keys whose values are free text written by users or models.
_TEXT_KEYS = frozenset(
    {
        "content",
        "text",
        "input",
        "output",
        "messages",
        "result",
        "justification",
        "description",
        "comment",
        "query",
        "answer",
        "instruction",
        "title",
        "generations",
        "args",
        "arguments",
    }
)


def enabled() -> bool:
    flag = os.environ.get("LANGSMITH_TRACING", "").lower() in {"1", "true", "yes"}
    return flag and bool(os.environ.get("LANGSMITH_API_KEY"))


def redact_payload(payload: Any) -> Any:
    """Keep structure and identifiers; replace free text."""
    if isinstance(payload, dict):
        return {
            k: (REDACTED if k in _TEXT_KEYS and _has_text(v) else redact_payload(v))
            for k, v in payload.items()
        }
    if isinstance(payload, list | tuple):
        return [redact_payload(v) for v in payload]
    if isinstance(payload, str):
        return scrub(payload)
    return payload


def _has_text(value: Any) -> bool:
    return isinstance(value, str | list | dict) and bool(value)


def callbacks() -> list[Any]:
    """The LangSmith tracer to add to a run's callbacks, or none."""
    if not enabled():
        return []
    from langchain_core.tracers.langchain import LangChainTracer
    from langsmith import Client

    client = Client(hide_inputs=redact_payload, hide_outputs=redact_payload)
    return [LangChainTracer(client=client, project_name=os.environ.get("LANGSMITH_PROJECT"))]
