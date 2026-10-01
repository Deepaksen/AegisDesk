"""Traces, metrics, logs and their redaction (Milestone 8), with in-memory exporters."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.approvals.service import ApprovalService
from aegisdesk.audit.events import InMemoryAuditLog
from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.graphs.service_desk_graph import ThreadedGraphAgent
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.observability import faults, langsmith, tracing
from aegisdesk.observability.logging import JsonFormatter, log_context
from aegisdesk.observability.redaction import (
    REDACTED,
    RedactingSpanProcessor,
    pseudonym,
    redact_attributes,
)
from aegisdesk.observability.setup import TelemetryExporter, configure_telemetry
from aegisdesk.observability.tree import render
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.transport import ToolFactory

FINANCE = "Please create an access request for FinanceERP for month-end reporting"
SECRET = "sk-ant-api03-DONOTLEAK1234567890"


@dataclass
class Telemetry:
    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader

    def finished(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def named(self, prefix: str) -> list[ReadableSpan]:
        return [s for s in self.finished() if s.name.startswith(prefix)]

    def points(self, metric: str) -> list[tuple[dict[str, Any], float]]:
        data = self.metrics.get_metrics_data()
        found: list[tuple[dict[str, Any], float]] = []
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for m in scope.metrics:
                    if m.name == metric:
                        for p in m.data.data_points:
                            value = getattr(p, "value", None)
                            if value is None:  # histogram: the number of observations
                                value = getattr(p, "count", 0)
                            found.append((dict(p.attributes or {}), float(value or 0)))
        return found

    def total(self, metric: str, **labels: str) -> float:
        return sum(
            v
            for attrs, v in self.points(metric)
            if all(attrs.get(k) == x for k, x in labels.items())
        )


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    configure_telemetry(service_name="aegisdesk-test", span_exporter=spans, metric_reader=reader)
    yield Telemetry(spans, reader)
    configure_telemetry(service_name="aegisdesk-test")


def _agent(
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    factory: ToolFactory | None = None,
) -> ThreadedGraphAgent:
    return build_supervisor_agent(
        Settings(),
        repository,
        checkpointer=InMemorySaver(),
        retriever=retriever,
        tool_factory=factory or ToolFactory(gateway=gateway),
    )


def _parent_names(span: ReadableSpan, spans: list[ReadableSpan]) -> list[str]:
    by_id = {s.context.span_id: s for s in spans if s.context}
    names, current = [], span
    while current.parent and current.parent.span_id in by_id:
        current = by_id[current.parent.span_id]
        names.append(current.name)
    return names


def test_one_request_is_one_trace_with_the_expected_shape(
    telemetry: Telemetry,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    aisha: UserContext,
    audit_log: InMemoryAuditLog,
) -> None:
    run = _agent(repository, retriever, gateway).run(FINANCE, user=aisha, thread_id="t-1")
    spans = telemetry.finished()

    assert run.trace_id and len({s.context.trace_id for s in spans if s.context}) == 1
    [root] = telemetry.named("aegisdesk.request")
    assert format(root.context.trace_id, "032x") == run.trace_id
    attrs = root.attributes or {}
    assert attrs[tracing.REQUEST_ID] == run.request_id and attrs[tracing.THREAD_ID] == "t-1"
    assert attrs[tracing.AGENT_NAME] == "supervisor" and attrs[tracing.GRAPH_STEPS] >= 4
    assert attrs[tracing.USER] == pseudonym("E1004")  # pseudonymous, never the raw ID
    assert attrs["aegisdesk.approval.pending"] == ("AP-0001",)

    [tool] = telemetry.named("execute_tool create_access_request")
    assert _parent_names(tool, spans)[:2] == ["invoke_agent access", "aegisdesk.request"]
    [policy] = telemetry.named("policy.evaluate")
    assert (policy.attributes or {})["aegisdesk.policy.decision"] == "allow"
    llm = telemetry.named("chat")
    assert len(llm) == 3  # router + two access-agent model calls
    assert all((s.attributes or {})[tracing.INPUT_TOKENS] > 0 for s in llm)
    assert {(s.attributes or {})[tracing.PROMPT] for s in llm} == {"router@v1", "access@v1"}
    assert telemetry.named("approval.await")

    # Audit events point back to the trace.
    assert {e.trace_id for e in audit_log.events} == {run.trace_id}


def test_nothing_sensitive_reaches_span_attributes(
    telemetry: Telemetry,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    aisha: UserContext,
) -> None:
    message = f"Please create an access request for FinanceERP, my key is {SECRET} thanks"
    run = _agent(repository, retriever, gateway).run(message, user=aisha)

    values = [
        str(v)
        for s in telemetry.finished()
        for v in [*(s.attributes or {}).values(), s.status.description or ""]
    ]
    assert not any(SECRET in v for v in values)
    assert not any("my key is" in v for v in values)  # no user text at all
    assert not any(run.answer[:40] in v for v in values)  # no model output
    assert not any("E1004" in v for v in values)  # no raw employee ID


def test_redacting_processor_drops_unknown_keys_and_scrubs_values() -> None:
    kept = redact_attributes(
        {
            "aegisdesk.note": f"token {SECRET} and Bearer abcdefghijklmnop",
            "gen_ai.prompt": "the whole prompt",
            "user.email": "aisha@example.com",
            "gen_ai.tool.name": "get_my_assets",
        }
    )

    assert kept == {
        "aegisdesk.note": f"token {REDACTED} and {REDACTED}",
        "gen_ai.tool.name": "get_my_assets",
    }
    assert isinstance(RedactingSpanProcessor, type)


def test_metrics_follow_the_catalogue_without_user_labels(
    telemetry: Telemetry,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    aisha: UserContext,
) -> None:
    _agent(repository, retriever, gateway).run(FINANCE, user=aisha)

    assert telemetry.total("aegisdesk.requests", agent="supervisor") == 1
    assert telemetry.total("aegisdesk.tool.calls", tool="create_access_request", status="ok") == 1
    assert telemetry.total("aegisdesk.llm.calls") == 3
    assert telemetry.total("aegisdesk.agent.invocations", agent="access") == 1
    assert telemetry.total("aegisdesk.approval.requests", step="manager") == 1
    assert telemetry.total("aegisdesk.tokens.input") > 0
    assert telemetry.total("aegisdesk.task.latency") == 1  # histogram count
    labels = {
        k
        for m in ("aegisdesk.requests", "aegisdesk.tool.calls")
        for a, _ in telemetry.points(m)
        for k in a
    }
    assert not labels & {"user", "user_id", "employee_id"}


def test_policy_denial_is_counted_by_reason(
    telemetry: Telemetry,
    repository: ServiceDeskRepository,
    gateway: ActionGateway,
    aisha: UserContext,
) -> None:
    from aegisdesk.identity.agent import AgentIdentity
    from aegisdesk.tools.executor import ToolExecutor
    from aegisdesk.tools.service_desk import build_service_desk_tools

    knowledge = AgentIdentity("knowledge", "0.1.0", "specialist", "development")
    ToolExecutor(build_service_desk_tools(repository), gateway=gateway, agent=knowledge).execute(
        "get_my_assets", {}, user=aisha, request_id="r"
    )

    assert (
        telemetry.total(
            "aegisdesk.policy.denials", tool="get_my_assets", reason="agent_not_authorized_for_tool"
        )
        == 1
    )
    [span] = telemetry.named("execute_tool get_my_assets")
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})[tracing.ERROR_CATEGORY] == "policy_denied"


def test_trace_continues_across_the_mcp_boundary(
    telemetry: Telemetry,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    aisha: UserContext,
) -> None:
    settings = Settings(tool_transport=ToolTransport.MCP_INPROCESS)
    with ToolFactory.from_settings(settings, repository, gateway=gateway) as factory:
        _agent(repository, retriever, gateway, factory).run(
            "What laptop is assigned to me?", user=aisha
        )
    spans = telemetry.finished()

    [server] = telemetry.named("mcp.server get_my_assets")
    [client] = telemetry.named("mcp.call read/get_my_assets")
    assert server.context.trace_id == client.context.trace_id
    assert "mcp.call read/get_my_assets" in _parent_names(server, spans)
    # Counted once, where the tool ran (the server), not again by the client.
    assert telemetry.total("aegisdesk.tool.calls", tool="get_my_assets") == 1


# -- the debugging exercise: injected failures are visible in telemetry ----------


def test_injected_tool_error_is_visible_in_spans_metrics_and_logs(
    telemetry: Telemetry,
    monkeypatch: pytest.MonkeyPatch,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    aisha: UserContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(faults.ENV, "tool_error:get_my_assets")
    faults.reload()

    run = _agent(repository, retriever, gateway).run("What laptop is assigned to me?", user=aisha)

    [tool] = telemetry.named("execute_tool get_my_assets")
    assert tool.status.status_code is StatusCode.ERROR
    assert (tool.attributes or {})[tracing.ERROR_CATEGORY] == "internal_error"
    assert (
        telemetry.total("aegisdesk.tool.errors", tool="get_my_assets", category="internal_error")
        == 1
    )
    [record] = [r for r in caplog.records if "failed" in r.getMessage()]
    line = json.loads(JsonFormatter().format(record))
    assert line["exception.type"] == "InjectedFaultError"
    assert run.trace_id is not None


def test_injected_mcp_timeout_is_visible_and_not_retried_for_writes(
    telemetry: Telemetry,
    monkeypatch: pytest.MonkeyPatch,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    aisha: UserContext,
) -> None:
    monkeypatch.setenv(faults.ENV, "tool_timeout:get_my_assets")
    faults.reload()
    settings = Settings(tool_transport=ToolTransport.MCP_INPROCESS)
    with ToolFactory.from_settings(settings, repository, gateway=gateway) as factory:
        _agent(repository, retriever, gateway, factory).run(
            "What laptop is assigned to me?", user=aisha
        )

    calls = telemetry.named("mcp.call read/get_my_assets")
    assert len(calls) == 2  # a read: one retry
    assert all((c.attributes or {})[tracing.ERROR_CATEGORY] == "timeout" for c in calls)
    assert not telemetry.named("mcp.server")  # never reached the server
    assert telemetry.total("aegisdesk.tool.errors", tool="get_my_assets", category="timeout") == 1


def test_injected_retrieval_failure_marks_the_retrieval_span(
    telemetry: Telemetry,
    monkeypatch: pytest.MonkeyPatch,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    aisha: UserContext,
) -> None:
    monkeypatch.setenv(faults.ENV, "retrieval_error")
    faults.reload()

    _agent(repository, retriever, gateway).run("How do I configure VPN on macOS?", user=aisha)

    [retrieval] = telemetry.named("rag.retrieve")
    assert retrieval.status.status_code is StatusCode.ERROR
    [tool] = telemetry.named("execute_tool search_knowledge_base")
    assert (tool.attributes or {})[tracing.ERROR_CATEGORY] == "internal_error"


def test_retrieval_span_records_document_ids_not_text(
    telemetry: Telemetry, retriever: Retriever, aisha: UserContext
) -> None:
    retriever.retrieve("How do I configure VPN on macOS?", aisha)

    [span] = telemetry.named("rag.retrieve")
    attrs = span.attributes or {}
    assert "DOC-VPN-001" in list(attrs["aegisdesk.rag.document_ids"])
    assert attrs["aegisdesk.rag.no_evidence"] is False
    assert not any("macOS" in str(v) for v in attrs.values())


def test_approval_decision_and_resume_are_traced(
    telemetry: Telemetry,
    repository: ServiceDeskRepository,
    retriever: Retriever,
    gateway: ActionGateway,
    aisha: UserContext,
    audit_log: InMemoryAuditLog,
) -> None:
    agent = _agent(repository, retriever, gateway)
    run = agent.run(FINANCE, user=aisha, thread_id="t-a")
    service = ApprovalService(repository.access_store, audit_log, environment="development")
    service.decide(
        run.pending_approvals[0]["approval_id"], authenticate(repository, "E1010"), approve=False
    )
    resumed = agent.resume("t-a")

    [decide] = telemetry.named("approval.decide")
    assert (decide.attributes or {})["aegisdesk.approval.decision"] == "reject"
    assert telemetry.named("aegisdesk.resume") and telemetry.named("approval.apply")
    assert telemetry.total("aegisdesk.approval.rejections", reason="rejected") == 1
    assert resumed.trace_id and resumed.trace_id != run.trace_id  # a new request, same thread


def test_json_logs_join_the_trace_and_scrub_secrets(telemetry: Telemetry) -> None:
    logger = logging.getLogger("aegisdesk.test")
    with (
        tracing.span("work") as current,
        log_context(request_id="r-1", thread_id="t-1", agent="access"),
    ):
        record = logger.makeRecord(
            logger.name, logging.WARNING, __file__, 1, f"key {SECRET}", (), None
        )
        line = json.loads(JsonFormatter().format(record))

    assert line["trace_id"] == format(current.get_span_context().trace_id, "032x")
    assert (line["request_id"], line["thread_id"], line["agent"]) == ("r-1", "t-1", "access")
    assert SECRET not in line["message"] and REDACTED in line["message"]


def test_tree_exporter_prints_the_span_tree() -> None:
    configure_telemetry(service_name="aegisdesk-test", exporter=TelemetryExporter.TREE)
    from aegisdesk.observability.setup import tree_exporter

    with tracing.span("outer"), tracing.span("inner", **{tracing.STATUS: "ok"}):
        pass
    exporter = tree_exporter()
    assert exporter is not None
    text = render(exporter.spans)
    configure_telemetry(service_name="aegisdesk-test")

    assert "└─ outer" in text and "└─ inner" in text and "status=ok" in text


# -- LangSmith ---------------------------------------------------------------------


def test_langsmith_payloads_are_redacted() -> None:
    payload = {
        "messages": [{"content": "my secret plan"}],
        "metadata": {"agent": "access", "request_id": "r-1"},
        "outputs": {"answer": "Access granted"},
        "note": f"key {SECRET}",
    }

    redacted = langsmith.redact_payload(payload)

    assert redacted["messages"] == REDACTED
    assert redacted["outputs"] == {"answer": REDACTED}
    assert redacted["metadata"] == {"agent": "access", "request_id": "r-1"}
    assert SECRET not in json.dumps(redacted)


def test_langsmith_is_off_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert langsmith.callbacks() == []  # no API key: off


def test_langsmith_tracer_is_added_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import langsmith as langsmith_sdk

    made: dict[str, Any] = {}

    class FakeClient:  # stands in for langsmith.Client: no network in tests
        def __init__(self, **kwargs: Any) -> None:
            made.update(kwargs)

    monkeypatch.setattr(langsmith_sdk, "Client", FakeClient)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_fake_key_for_tests")

    [tracer] = langsmith.callbacks()

    assert type(tracer).__name__ == "LangChainTracer"
    # Inputs and outputs go through our redaction before anything could be sent.
    assert made["hide_inputs"] is langsmith.redact_payload
    assert made["hide_outputs"] is langsmith.redact_payload
