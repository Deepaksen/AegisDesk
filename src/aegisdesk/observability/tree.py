"""Print a trace as an indented tree (the `tree` exporter; `aegisdesk agent --trace`)."""

from __future__ import annotations

from collections import defaultdict

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

# Shown after the span name, in this order, when present: attribute -> label.
_SHOWN = {
    "gen_ai.usage.input_tokens": "in",
    "gen_ai.usage.output_tokens": "out",
    "aegisdesk.prompt": "prompt",
    "aegisdesk.status": "status",
    "aegisdesk.policy.decision": "decision",
    "aegisdesk.policy.reasons": "reasons",
    "aegisdesk.approval.ids": "approvals",
    "aegisdesk.approval.pending": "pending",
    "aegisdesk.approval.decision": "decision",
    "aegisdesk.rag.document_ids": "docs",
    "aegisdesk.rag.no_evidence": "no_evidence",
    "aegisdesk.error.category": "error",
    "aegisdesk.graph.steps": "steps",
}


def render(spans: list[ReadableSpan], trace_id: int | None = None) -> str:
    """The spans of one trace (the latest, by default) as a tree."""
    if not spans:
        return "(no spans)"
    trace_id = trace_id if trace_id is not None else spans[-1].context.trace_id
    mine = [s for s in spans if s.context and s.context.trace_id == trace_id]
    ids = {s.context.span_id for s in mine if s.context}
    children: dict[int | None, list[ReadableSpan]] = defaultdict(list)
    for s in mine:
        parent = s.parent.span_id if s.parent and s.parent.span_id in ids else None
        children[parent].append(s)
    for group in children.values():
        group.sort(key=lambda s: s.start_time or 0)

    lines = [f"trace {trace_id:032x}"]

    def walk(parent: int | None, prefix: str) -> None:
        group = children.get(parent, [])
        for i, s in enumerate(group):
            last = i == len(group) - 1
            ms = ((s.end_time or 0) - (s.start_time or 0)) / 1e6
            attrs = s.attributes or {}
            details = " ".join(
                f"{label}={_short(attrs[k])}"
                for k, label in _SHOWN.items()
                if k in attrs and attrs[k] not in ("", ())
            )
            error = " ✗" if s.status.status_code is StatusCode.ERROR else ""
            lines.append(f"{prefix}{'└─' if last else '├─'} {s.name} {ms:.1f}ms{error} {details}")
            walk(s.context.span_id if s.context else None, prefix + ("   " if last else "│  "))

    walk(None, "")
    return "\n".join(line.rstrip() for line in lines)


def _short(value: object) -> str:
    if isinstance(value, tuple | list):
        return ",".join(str(v) for v in value)
    return str(value)
