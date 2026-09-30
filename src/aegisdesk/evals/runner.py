"""Run golden cases against one version of the system and record everything observable.

For each case the runner builds a fresh world (seeded repository and access
store, audit log, checkpointer, in-memory telemetry), runs the request (and
any scripted approvals and resumes), and returns a `CaseRun`:

* the agent runs (answers, trajectories, pending approvals);
* audit events (policy decisions, approvals);
* finished spans (model calls, tools, retrieval, latency, tokens);
* JSON log lines (to check that secrets never reach logs);
* the effects: what was written to the data stores.

Evaluators (`evaluators.py`) then judge the `CaseRun` in plain code.

System versions ("configs") compared by the spec's "compare two versions":

* `multi`      supervisor + specialists (M4-M8), tools in-process
* `multi_mcp`  the same with enterprise tools over MCP (in-process transport)
* `single`     the single Service Desk agent (M2/M3 engine, prompt v2)

The model comes from the usual settings (MODEL_PROVIDER / MODEL_NAME), so the
same runner compares models when credentials are available.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aegisdesk.agents.loop import AgentRun
from aegisdesk.agents.service_desk import build_service_desk_graph_agent
from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.approvals.service import ApprovalError, ApprovalService
from aegisdesk.audit.events import AuditEvent, InMemoryAuditLog
from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.evals.golden import GoldenCase, ScriptStep
from aegisdesk.governance.factory import build_gateway
from aegisdesk.graphs.service_desk_graph import ThreadedGraphAgent
from aegisdesk.identity.context import authenticate
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.observability import faults
from aegisdesk.observability.logging import JsonFormatter
from aegisdesk.observability.setup import configure_telemetry
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.transport import ToolFactory

logger = logging.getLogger(__name__)


class SystemConfig(StrEnum):
    MULTI = "multi"
    MULTI_MCP = "multi_mcp"
    SINGLE = "single"


@dataclass(frozen=True)
class Snapshot:
    tickets: frozenset[str]
    comments: frozenset[str]
    requests: frozenset[str]
    grants: frozenset[tuple[str, str, date]]

    @classmethod
    def take(cls, repository: ServiceDeskRepository) -> Snapshot:
        employees = repository.list_employee_ids()
        tickets = {t.ticket_id for e in employees for t in repository.list_tickets_for(e)}
        return cls(
            tickets=frozenset(tickets),
            comments=frozenset(c.comment_id for t in tickets for c in repository.comments_for(t)),
            requests=frozenset(
                r.request_id for e in employees for r in repository.access_requests_for(e)
            ),
            grants=frozenset(
                (a.employee_id, a.application_id, a.granted_on)
                for e in employees
                for a in repository.access_for(e)
            ),
        )


@dataclass(frozen=True)
class ObservedEffects:
    tickets_created: int
    comments_added: int
    access_requests_created: int
    access_granted: list[str]  # "E1004:APP-FIN"

    @classmethod
    def between(cls, before: Snapshot, after: Snapshot) -> ObservedEffects:
        return cls(
            tickets_created=len(after.tickets - before.tickets),
            comments_added=len(after.comments - before.comments),
            access_requests_created=len(after.requests - before.requests),
            access_granted=sorted(f"{e}:{a}" for e, a, _ in after.grants - before.grants),
        )


@dataclass(frozen=True)
class RequestAudit:
    """What happened to one access request created during the case."""

    request_id: str
    employee_id: str
    application_id: str
    steps: int  # approval steps required
    all_approved: bool
    auto_approved: bool
    granted: bool


@dataclass
class CaseRun:
    case: GoldenCase
    config: SystemConfig
    model: str
    skipped: str | None = None
    error: str | None = None
    runs: list[AgentRun] = field(default_factory=list)
    first_pending: list[dict[str, Any]] = field(default_factory=list)
    audit: list[AuditEvent] = field(default_factory=list)
    spans: list[ReadableSpan] = field(default_factory=list)
    logs: str = ""
    effects: ObservedEffects | None = None
    requests: list[RequestAudit] = field(default_factory=list)
    latency_s: float = 0.0

    @property
    def answer(self) -> str:
        return "\n\n".join(r.answer for r in self.runs)


def script_messages(script: list[ScriptStep]) -> list[AIMessage]:
    messages: list[AIMessage] = []
    for i, step in enumerate(script):
        if step.route is not None:
            plan = {"tasks": [r.model_dump() for r in step.route], "out_of_scope": False}
            messages.append(
                AIMessage(
                    content="", tool_calls=[{"name": "RoutingPlan", "args": plan, "id": f"r{i}"}]
                )
            )
        elif step.tool_calls is not None:
            calls = [
                {"name": c.name, "args": c.args, "id": f"s{i}c{j}"}
                for j, c in enumerate(step.tool_calls)
            ]
            messages.append(AIMessage(content="", tool_calls=calls))
        else:
            messages.append(AIMessage(content=step.text or ""))
    return messages


@contextmanager
def _faults(spec: str | None) -> Iterator[None]:
    previous = os.environ.get(faults.ENV)
    if spec:
        os.environ[faults.ENV] = spec
    else:
        os.environ.pop(faults.ENV, None)
    faults.reload()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(faults.ENV, None)
        else:
            os.environ[faults.ENV] = previous
        faults.reload()


@contextmanager
def _captured_logs() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(min(previous, logging.INFO) if previous else logging.INFO)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _request_audit(
    repository: ServiceDeskRepository, before: Snapshot, after: Snapshot
) -> list[RequestAudit]:
    store = repository.access_store
    audits = []
    for request_id in sorted(after.requests - before.requests):
        request = store.get_access_request(request_id)
        if request is None:
            continue
        approvals = store.approvals_for_request(request_id)
        audits.append(
            RequestAudit(
                request_id=request_id,
                employee_id=request.employee_id,
                application_id=request.application_id,
                steps=len(approvals),
                all_approved=bool(approvals)
                and all(a.status.value == "approved" for a in approvals),
                auto_approved=request.status.value == "auto_approved",
                granted=request.provisioned_at is not None,
            )
        )
    return audits


class EvalRunner:
    def __init__(
        self,
        settings: Settings,
        retriever: Retriever,
        *,
        today: date,
        model_factory: Callable[[], BaseChatModel],
        model_label: str,
    ) -> None:
        self._settings = settings
        self._retriever = retriever
        self._today = today
        self._model_factory = model_factory
        self.model_label = model_label
        self._spans = InMemorySpanExporter()
        configure_telemetry(service_name="aegisdesk-eval", span_exporter=self._spans)

    def run(self, case: GoldenCase, config: SystemConfig) -> CaseRun:
        result = CaseRun(case=case, config=config, model=self.model_label)
        if case.setup.configs is not None and config.value not in case.setup.configs:
            result.skipped = f"not applicable to {config.value}"
            return result
        self._spans.clear()
        repository = ServiceDeskRepository.from_seed(
            self._settings.seed_data_dir,
            today=lambda: self._today,
            approval_ttl=timedelta(hours=self._settings.approval_ttl_hours),
        )
        audit = InMemoryAuditLog()
        gateway = build_gateway(self._settings, audit=audit, access_store=repository.access_store)
        model = (
            ScriptedChatModel(responses=script_messages(case.setup.script))
            if case.setup.script
            else self._model_factory()
        )
        transport = (
            ToolTransport.MCP_INPROCESS
            if config is SystemConfig.MULTI_MCP or case.setup.transport == "mcp_inprocess"
            else ToolTransport.LOCAL
        )
        settings = self._settings.model_copy(update={"tool_transport": transport})
        before = Snapshot.take(repository)
        with (
            _faults(case.setup.faults),
            _captured_logs() as logs,
            ToolFactory.from_settings(settings, repository, gateway=gateway) as factory,
        ):
            try:
                agent = self._build(config, settings, repository, model, factory, gateway)
                self._execute(case, agent, repository, audit, result)
            except Exception as exc:  # a crash is a failed case, not a failed suite
                logger.exception("eval case %s crashed", case.id)
                result.error = f"{type(exc).__name__}: {exc}"
        # End-to-end latency of the requests themselves (not building the system).
        result.latency_s = sum(r.latency_ms for r in result.runs) / 1000
        result.logs = logs.getvalue()
        result.audit = audit.events
        result.spans = list(self._spans.get_finished_spans())
        after = Snapshot.take(repository)
        result.effects = ObservedEffects.between(before, after)
        result.requests = _request_audit(repository, before, after)
        return result

    def _build(
        self,
        config: SystemConfig,
        settings: Settings,
        repository: ServiceDeskRepository,
        model: BaseChatModel,
        factory: ToolFactory,
        gateway: Any,
    ) -> ThreadedGraphAgent:
        if config is SystemConfig.SINGLE:
            return build_service_desk_graph_agent(
                settings,
                repository,
                checkpointer=InMemorySaver(),
                model=model,
                retriever=self._retriever,
                gateway=gateway,
            )
        return build_supervisor_agent(
            settings,
            repository,
            checkpointer=InMemorySaver(),
            model=model,
            retriever=self._retriever,
            tool_factory=factory,
        )

    def _execute(
        self,
        case: GoldenCase,
        agent: ThreadedGraphAgent,
        repository: ServiceDeskRepository,
        audit: InMemoryAuditLog,
        result: CaseRun,
    ) -> None:
        user = authenticate(repository, case.user.employee_id)
        thread_id = f"eval-{case.id}"
        request_id = f"eval-{case.id}-request"
        for _ in range(case.setup.repeat):  # repeated submission: same request ID
            result.runs.append(
                agent.run(case.text, user=user, thread_id=thread_id, request_id=request_id)
            )
        result.first_pending = list(result.runs[0].pending_approvals)

        service = ApprovalService(repository.access_store, audit, environment="development")
        for decision in case.setup.decisions:
            pending = agent.pending_approvals(thread_id)
            chosen = next((p for p in pending if decision.step in (None, p["step"])), None)
            if chosen is None:
                result.error = f"no pending approval for decision {decision.model_dump()}"
                return
            try:
                outcome = service.decide(
                    chosen["approval_id"],
                    authenticate(repository, decision.approver),
                    approve=decision.decision == "approve",
                    comment=decision.comment,
                )
            except ApprovalError as exc:
                if decision.expect_refusal == exc.category:
                    continue  # refused as the case expects (e.g. separation of duties)
                result.error = f"approval refused: {exc.category}"
                return
            if decision.expect_refusal is not None:
                result.error = f"decision by {decision.approver} was accepted, expected refusal"
                return
            if not outcome.request.status.is_open:
                result.runs.append(agent.resume(thread_id))
