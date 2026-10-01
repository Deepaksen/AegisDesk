"""W3C trace context across the MCP boundary.

The client puts `traceparent` (and `tracestate`) into the MCP request's
`_meta`, next to the delegation token; the server extracts it, so its spans
are children of the client's tool span, even across processes (mcp_http).
The trace context identifies a trace, not a person: it grants nothing and is
not trusted for anything but correlation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_propagator = TraceContextTextMapPropagator()
TRACE_KEYS = ("traceparent", "tracestate")


def inject(meta: dict[str, Any]) -> dict[str, Any]:
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    meta.update({k: v for k, v in carrier.items() if k in TRACE_KEYS})
    return meta


def extract(meta: Mapping[str, Any] | None) -> Context:
    carrier = {k: str(v) for k, v in (meta or {}).items() if k in TRACE_KEYS}
    return _propagator.extract(carrier)
