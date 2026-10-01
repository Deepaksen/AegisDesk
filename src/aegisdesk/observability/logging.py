"""Structured JSON logs that join traces.

`LOG_FORMAT=json` makes every log record one JSON object with the current
`trace_id` / `span_id` (from OpenTelemetry) and `request_id` / `thread_id` /
`agent` (from context variables set by the request span). The same secret
scrubbing as for spans applies. Existing `logger.info(...)` calls need no
changes.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from opentelemetry import trace

from aegisdesk.observability.redaction import scrub

_request_id: ContextVar[str | None] = ContextVar("aegisdesk_request_id", default=None)
_thread_id: ContextVar[str | None] = ContextVar("aegisdesk_thread_id", default=None)
_agent: ContextVar[str | None] = ContextVar("aegisdesk_agent", default=None)


@contextmanager
def log_context(
    *, request_id: str | None = None, thread_id: str | None = None, agent: str | None = None
) -> Iterator[None]:
    tokens = [
        _request_id.set(request_id or _request_id.get()),
        _thread_id.set(thread_id or _thread_id.get()),
        _agent.set(agent or _agent.get()),
    ]
    try:
        yield
    finally:
        for var, token in zip((_request_id, _thread_id, _agent), tokens, strict=True):
            var.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = trace.get_current_span().get_span_context()
        body: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": scrub(record.getMessage()),
            "trace_id": format(context.trace_id, "032x") if context.is_valid else None,
            "span_id": format(context.span_id, "016x") if context.is_valid else None,
            "request_id": _request_id.get(),
            "thread_id": _thread_id.get(),
            "agent": _agent.get(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            # The exception type only: messages and stack traces can carry data.
            body["exception.type"] = record.exc_info[0].__name__
        return json.dumps({k: v for k, v in body.items() if v is not None})


def configure_logging(*, fmt: str = "text", level: str = "WARNING") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_aegisdesk", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler._aegisdesk = True  # type: ignore[attr-defined]
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
