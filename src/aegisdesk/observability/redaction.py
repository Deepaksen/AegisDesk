"""Redaction before telemetry leaves the process.

Two layers, so a mistake in one is caught by the other:

1. **What we record.** Instrumentation sets identifiers, names, counts,
   latencies and decisions, never message text, tool arguments or tool
   results (spec section 23: observe actions and outcomes, not content or
   reasoning).
2. **What we export.** `RedactingSpanProcessor` sits in front of every
   exporter. It drops attributes whose keys are not on the allowlist and
   scrubs secrets from the values it keeps. The collector config drops
   `gen_ai.prompt*` / `gen_ai.completion*` once more.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

MAX_VALUE_LENGTH = 200
REDACTED = "[REDACTED]"

# Keys (or key prefixes ending in ".") that may leave the process.
ALLOWED_KEYS = frozenset(
    {
        "aegisdesk.",
        "gen_ai.operation.name",
        "gen_ai.system",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.agent.name",
        "gen_ai.tool.name",
        "error.type",
        "exception.type",
        "service.name",
        "rpc.system",
        "rpc.method",
        "server.address",
    }
)

_SECRETS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic keys
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # other API keys
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWTs
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(password|secret|api[_-]?key)\s*[=:]\s*\S+"),
]


def allowed(key: str) -> bool:
    return key in ALLOWED_KEYS or any(
        key.startswith(prefix) for prefix in ALLOWED_KEYS if prefix.endswith(".")
    )


def scrub(value: str) -> str:
    for pattern in _SECRETS:
        value = pattern.sub(REDACTED, value)
    if len(value) > MAX_VALUE_LENGTH:
        value = value[:MAX_VALUE_LENGTH] + "…"
    return value


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return tuple(_clean(v) for v in value)
    return value


def redact_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: _clean(v) for k, v in (attributes or {}).items() if allowed(k)}


def pseudonym(identifier: str) -> str:
    """A stable, non-reversible stand-in for a person in telemetry (not in audit)."""
    return hashlib.sha256(f"aegisdesk:{identifier}".encode()).hexdigest()[:16]


class RedactingSpanProcessor(SpanProcessor):
    """Wraps the exporting processor; the exporter only ever sees redacted spans."""

    def __init__(self, inner: SpanProcessor) -> None:
        self._inner = inner

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        self._inner.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        # ReadableSpan is immutable; the SDK keeps attributes in a BoundedAttributes
        # mapping we can rebuild from. Events (exception messages) are scrubbed too.
        span._attributes = redact_attributes(span.attributes)
        span._events = tuple(
            type(e)(e.name, redact_attributes(e.attributes), e.timestamp) for e in span.events
        )
        if span.status.description:
            span._status = type(span.status)(
                span.status.status_code, scrub(span.status.description)
            )
        self._inner.on_end(span)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)
