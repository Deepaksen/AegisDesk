"""The metrics of spec section 24, as OpenTelemetry instruments.

Labels are low-cardinality and never identify a person: agent, tool, model,
status, error category, decision. (A user ID label would create one time
series per employee and leak identity into a monitoring system.)

Prometheus names are derived by the collector: `aegisdesk.tool.calls` becomes
`aegisdesk_tool_calls_total`, `aegisdesk.tool.latency` (seconds) becomes
`aegisdesk_tool_latency_seconds_bucket/_sum/_count`. Counter units are
annotations (`{call}`), which the Prometheus translation drops (unit `1`
could add a `_ratio` suffix).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry.metrics import Counter, Histogram

from aegisdesk.observability.setup import meter_provider


@dataclass(frozen=True)
class Instruments:
    requests: Counter
    requests_failed: Counter
    agent_invocations: Counter
    llm_calls: Counter
    tool_calls: Counter
    tool_errors: Counter
    input_tokens: Counter
    output_tokens: Counter
    approval_requests: Counter
    approval_rejections: Counter
    policy_denials: Counter
    rag_no_evidence: Counter
    tool_latency: Histogram
    llm_latency: Histogram
    task_latency: Histogram
    rag_latency: Histogram
    http_requests: Counter
    http_latency: Histogram
    model_errors: Counter
    circuit_transitions: Counter
    idempotency_replays: Counter


# (name, kind, unit, description): the single catalogue used by code, docs and tests.
CATALOGUE: list[tuple[str, str, str, str]] = [
    ("requests", "counter", "{request}", "User requests (turns and resumes) handled"),
    ("requests.failed", "counter", "{request}", "Requests that ended in an error"),
    ("agent.invocations", "counter", "{invocation}", "Specialist agent runs"),
    ("llm.calls", "counter", "{call}", "Model calls"),
    ("tool.calls", "counter", "{call}", "Tool executions, by status"),
    ("tool.errors", "counter", "{call}", "Tool executions that failed, by category"),
    ("tokens.input", "counter", "{token}", "Input tokens"),
    ("tokens.output", "counter", "{token}", "Output tokens"),
    ("approval.requests", "counter", "{step}", "Approval steps created"),
    ("approval.rejections", "counter", "{step}", "Approval steps rejected or expired"),
    ("policy.denials", "counter", "{call}", "Tool calls denied by policy, by reason"),
    ("rag.no_evidence", "counter", "{retrieval}", "Retrievals that found no usable evidence"),
    ("tool.latency", "histogram", "s", "Tool execution latency"),
    ("llm.latency", "histogram", "s", "Model call latency"),
    ("task.latency", "histogram", "s", "End-to-end request latency"),
    ("rag.retrieval.latency", "histogram", "s", "Retrieval latency"),
    ("http.requests", "counter", "{request}", "API requests, by method, route and status"),
    ("http.latency", "histogram", "s", "API request latency, by method and route"),
    ("model.errors", "counter", "{attempt}", "Failed model call attempts, by category"),
    ("circuit.transitions", "counter", "{transition}", "Circuit breakers opening and closing"),
    ("idempotency.replays", "counter", "{request}", "Duplicate requests answered from storage"),
]


def prometheus_names() -> set[str]:
    """The series names Prometheus will have (for dashboards and tests)."""
    names: set[str] = set()
    for name, kind, unit, _ in CATALOGUE:
        base = "aegisdesk_" + name.replace(".", "_")
        if kind == "counter":
            names.add(base + "_total")
        else:
            suffix = "_seconds" if unit == "s" else ""
            names |= {base + suffix + s for s in ("_bucket", "_sum", "_count")}
    return names


_instruments: Instruments | None = None


def reset() -> None:
    global _instruments
    _instruments = None


def instruments() -> Instruments:
    global _instruments
    if _instruments is None:
        meter = meter_provider().get_meter("aegisdesk")
        made: dict[str, Any] = {}
        for name, kind, unit, description in CATALOGUE:
            full = f"aegisdesk.{name}"
            if kind == "counter":
                made[name] = meter.create_counter(full, unit=unit, description=description)
            else:
                made[name] = meter.create_histogram(full, unit=unit, description=description)
        _instruments = Instruments(
            requests=made["requests"],
            requests_failed=made["requests.failed"],
            agent_invocations=made["agent.invocations"],
            llm_calls=made["llm.calls"],
            tool_calls=made["tool.calls"],
            tool_errors=made["tool.errors"],
            input_tokens=made["tokens.input"],
            output_tokens=made["tokens.output"],
            approval_requests=made["approval.requests"],
            approval_rejections=made["approval.rejections"],
            policy_denials=made["policy.denials"],
            rag_no_evidence=made["rag.no_evidence"],
            tool_latency=made["tool.latency"],
            llm_latency=made["llm.latency"],
            task_latency=made["task.latency"],
            rag_latency=made["rag.retrieval.latency"],
            http_requests=made["http.requests"],
            http_latency=made["http.latency"],
            model_errors=made["model.errors"],
            circuit_transitions=made["circuit.transitions"],
            idempotency_replays=made["idempotency.replays"],
        )
    return _instruments
