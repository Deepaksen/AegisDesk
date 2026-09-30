"""The application runtime: one long-lived set of components per process (Milestone 10).

The CLI used to build the repository, audit log, approval service, checkpointer,
tool factory and supervisor agent separately for each command. A server needs
them once, shared by every request, so they live here. The API and the CLI's
approval commands both use `AegisRuntime`: one code path for "decide, then
resume the paused workflow".

This module also turns what the system did into things that are safe to show
an employee: activity summaries ("Checking your employee profile..."), citations
and references. Nothing here exposes model text other than the final answer,
raw tool results, or tool arguments chosen by the model, except identifiers
that match a known format or resolve against the directory.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy import create_engine, text

from aegisdesk.agents.loop import (
    AgentRun,
    AgentStep,
    ModelStep,
    RouteStep,
    StopReason,
    ToolStep,
    TrajectoryStep,
)
from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.approvals.service import ApprovalService, DecisionResult
from aegisdesk.approvals.workflow import workflow_identity
from aegisdesk.audit.events import AuditLog
from aegisdesk.config import (
    AuditStoreKind,
    CheckpointStoreKind,
    DataStoreKind,
    Settings,
    VectorStoreKind,
)
from aegisdesk.domain.access import AccessRequest
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.factory import build_audit_log, build_gateway
from aegisdesk.graphs.service_desk_graph import (
    NODE_START,
    NotPausedError,
    ThreadedGraphAgent,
    step_from_entry,
)
from aegisdesk.identity.context import AuthenticationError, UserContext, authenticate
from aegisdesk.persistence.factory import (
    build_idempotency_store,
    build_repository,
    open_checkpointer,
)
from aegisdesk.persistence.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from aegisdesk.rag.ingestion.loader import load_document
from aegisdesk.reliability.breaker import breaker_states
from aegisdesk.reliability.model_guard import OUTAGE
from aegisdesk.tools.executor import OutcomeStatus, ToolRunner
from aegisdesk.tools.provisioning import PROVISION_TOOL, PROVISIONABLE, build_provisioning_tools
from aegisdesk.tools.transport import ToolFactory

CITATION = re.compile(r"\bDOC-[A-Z]+-\d{3}\b")
REFERENCE = re.compile(r"\b(?:INC|AR|AP)-\d{4,}\b")
TICKET_ID = re.compile(r"^INC-\d{4,}$")
DOCUMENT_ID = re.compile(r"^DOC-[A-Z]+-\d{3}$")
WRITE_TOOLS = frozenset({"create_ticket", "add_ticket_comment", "create_access_request"})

# -- safe activity summaries ----------------------------------------------------------

AGENT_LABELS = {
    "knowledge": "knowledge",
    "service_desk": "service desk",
    "access": "access",
}
STEP_LABELS = {
    "manager": "Manager",
    "data_owner": "Data owner",
    "security": "Security",
    "it_admin": "IT admin",
}


@dataclass(frozen=True)
class Activity:
    text: str
    status: str = "ok"  # ok | error | waiting


def _safe_ticket(value: object) -> str | None:
    text_value = str(value).strip().upper()
    return text_value if TICKET_ID.match(text_value) else None


def _tool_activity(step: ToolStep, repository: ServiceDeskRepository) -> Activity:
    args, name = step.args, step.tool_name

    def application() -> str:
        # Model-chosen text is shown only once it resolves to a real application.
        app = repository.find_application(str(args.get("application", "")))
        return app.name if app else "the application"

    created = REFERENCE.findall(step.result) if step.status is OutcomeStatus.OK else []
    texts: dict[str, Callable[[], str]] = {
        "search_knowledge_base": lambda: "Searching the knowledge base...",
        "retrieve_document": lambda: (
            f"Reading {args['document_id']}..."
            if DOCUMENT_ID.match(str(args.get("document_id", "")))
            else "Reading a document..."
        ),
        "get_my_assets": lambda: "Checking your assigned assets...",
        "list_my_tickets": lambda: "Checking your tickets...",
        "get_ticket": lambda: f"Looking up ticket {_safe_ticket(args.get('ticket_id')) or ''}...",
        "create_ticket": lambda: (
            f"Ticket {created[0]} created." if created else "Creating a support ticket..."
        ),
        "add_ticket_comment": lambda: (
            f"Adding a comment to {_safe_ticket(args.get('ticket_id')) or 'a ticket'}..."
        ),
        "get_employee_profile": lambda: "Checking your employee profile...",
        "list_my_access": lambda: "Checking your current access...",
        "get_application": lambda: f"Looking up {application()}...",
        "check_access_eligibility": lambda: f"Reviewing {application()} access policy...",
        "create_access_request": lambda: (
            f"Access request {created[0]} created."
            if created
            else f"Creating an access request for {application()}..."
        ),
        "request_handoff": lambda: (
            f"Handing over to the {AGENT_LABELS.get(str(args.get('target_agent')), 'right')} "
            "specialist..."
        ),
        "provision_access": lambda: "Approved access provisioned.",
    }
    describe = texts.get(name)
    if describe is None:  # an unknown, model-invented name is never echoed
        return Activity("An unavailable action was refused.", "error")
    summary = describe().replace(" ...", "...")
    if step.status is OutcomeStatus.OK:
        return Activity(summary)
    reason = (step.error_category or "error").replace("_", " ")
    return Activity(f"{summary.rstrip('.')} - not completed ({reason}).", "error")


def activity_from_step(step: TrajectoryStep, repository: ServiceDeskRepository) -> Activity | None:
    if isinstance(step, RouteStep):
        if (step.error or "").startswith(ROUTER_MODEL_ERROR):
            return Activity("The assistant is not responding right now.", "error")
        if step.error:
            return Activity("Could not understand the request.", "error")
        if step.out_of_scope:
            return Activity("This request is outside what the service desk handles.")
        agents = ", ".join(AGENT_LABELS.get(a, a) for a, _ in step.tasks)
        return Activity(f"Routing your request to: {agents}.")
    if isinstance(step, ToolStep):
        return _tool_activity(step, repository)
    if isinstance(step, AgentStep) and step.status != "done":  # incomplete | failed
        label = AGENT_LABELS.get(step.agent, step.agent)
        return Activity(f"The {label} specialist could not finish.", "error")
    if isinstance(step, ModelStep) and step.error in OUTAGE | {"model_error"}:
        return Activity("The assistant is not responding right now.", "error")
    return None  # model steps: never shown (no chain-of-thought)


def approval_activity(pending: list[dict[str, Any]]) -> list[Activity]:
    steps = dict.fromkeys(str(p["step"]) for p in pending)  # ordered, unique
    return [
        Activity(
            f"{STEP_LABELS.get(s, s.replace('_', ' ').capitalize())} approval is required.",
            "waiting",
        )
        for s in steps
    ]


class ActivityStream:
    """`on_update` callback: turns graph node updates into new activity items as they happen."""

    def __init__(self, repository: ServiceDeskRepository, emit: Callable[[Activity], None]) -> None:
        self._repository = repository
        self._emit = emit
        self._seen = 0

    def __call__(self, node: str, update: dict[str, Any]) -> None:
        trajectory = update.get("trajectory")
        if trajectory is None:
            return
        if node == NODE_START:
            self._seen = 0
            return
        for entry in trajectory[self._seen :]:
            item = activity_from_step(step_from_entry(entry), self._repository)
            if item is not None:
                self._emit(item)
        self._seen = len(trajectory)


# -- results ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    document_id: str
    title: str


@dataclass
class TurnResult:
    """What a client may see about one run (a turn or a resume)."""

    thread_id: str
    request_id: str
    trace_id: str | None
    answer: str
    stop_reason: str
    activity: list[Activity]
    citations: list[Citation]
    references: list[str]
    pending_approvals: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    latency_ms: float
    # M11: why the model failed this turn (model_timeout, circuit_open...), if it did.
    model_error: str | None = None

    @property
    def model_outage(self) -> bool:
        return self.model_error in OUTAGE


@dataclass(frozen=True)
class ThreadMessage:
    role: str  # user | assistant
    text: str


@dataclass
class ThreadView:
    thread_id: str
    messages: list[ThreadMessage]
    pending_approvals: list[dict[str, Any]]


@dataclass
class Decision:
    result: DecisionResult
    resumed: TurnResult | None = None
    note: str | None = None


@dataclass(frozen=True)
class Reconciled:
    request_id: str
    action: str  # resumed | provisioned | failed | skipped
    detail: str | None


@dataclass
class Readiness:
    components: dict[str, str] = field(default_factory=dict)
    # Informational (M11): an open circuit means degraded, not "not ready".
    circuits: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return all(v == "ok" for v in self.components.values())


def request_id_for(user: UserContext, idempotency_key: str | None) -> str | None:
    """A client retry with the same key maps to the same request ID (and so the same writes)."""
    if not idempotency_key:
        return None
    digest = hashlib.sha256(f"{user.employee_id}:{idempotency_key}".encode()).hexdigest()
    return f"idem-{digest[:32]}"


class AegisRuntime:
    def __init__(
        self,
        settings: Settings,
        repository: ServiceDeskRepository,
        audit: AuditLog,
        agent: ThreadedGraphAgent,
        document_titles: dict[str, str],
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self.settings = settings
        self.idempotency = idempotency or InMemoryIdempotencyStore()
        self.provisioner: ToolRunner | None = None
        self.repository = repository
        self.audit = audit
        self.agent = agent
        self.approvals = ApprovalService(
            repository.access_store, audit, environment=settings.aegis_env.value
        )
        self._titles = document_titles
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @classmethod
    @contextmanager
    def open(cls, settings: Settings, **overrides: Any) -> Iterator[AegisRuntime]:
        """Build everything once; close checkpointer and tool transport on exit.

        One audit log is shared by the tool gateway and the approval service, so
        decisions, approvals and provisioning land in the same trail.
        """
        repository = overrides.pop("repository", None) or build_repository(settings)
        audit = overrides.pop("audit", None) or build_audit_log(settings)
        gateway = build_gateway(settings, audit=audit, access_store=repository.access_store)
        with ExitStack() as stack:
            checkpointer = stack.enter_context(open_checkpointer(settings))
            factory = stack.enter_context(
                ToolFactory.from_settings(settings, repository, gateway=gateway)
            )
            agent = build_supervisor_agent(
                settings, repository, checkpointer=checkpointer, tool_factory=factory, **overrides
            )
            runtime = cls(
                settings,
                repository,
                audit,
                agent,
                _document_titles(settings),
                build_idempotency_store(settings),
            )
            # The deterministic workflow identity, the only one allowed to provision (M7).
            runtime.provisioner = factory.runner(
                workflow_identity(settings.aegis_env.value), build_provisioning_tools(repository)
            )
            yield runtime

    # -- conversation ----------------------------------------------------------------------

    @contextmanager
    def _thread_lock(self, thread_id: str) -> Iterator[None]:
        # One turn at a time per thread: two concurrent turns would interleave state.
        with self._locks_guard:
            lock = self._locks.setdefault(thread_id, threading.Lock())
        with lock:
            yield

    def send_message(
        self,
        user: UserContext,
        thread_id: str,
        text_input: str,
        *,
        request_id: str | None = None,
        on_activity: Callable[[Activity], None] | None = None,
    ) -> TurnResult:
        stream = ActivityStream(self.repository, on_activity) if on_activity else None
        with self._thread_lock(thread_id):
            run = self.agent.run(
                text_input, user=user, thread_id=thread_id, request_id=request_id, on_update=stream
            )
        result = self.turn_result(run)
        if on_activity is not None:
            for item in approval_activity(run.pending_approvals):
                on_activity(item)
        return result

    def turn_result(self, run: AgentRun, *, first_step: int = 0) -> TurnResult:
        """`first_step`: skip trajectory steps already reported (a resume continues a turn)."""
        new_steps = run.trajectory[first_step:]
        activity = [
            item
            for step in new_steps
            if (item := activity_from_step(step, self.repository)) is not None
        ] + approval_activity(run.pending_approvals)
        cited = dict.fromkeys(CITATION.findall(run.answer))
        written = " ".join(
            s.result
            for s in new_steps
            if isinstance(s, ToolStep)
            and s.tool_name in WRITE_TOOLS
            and s.status is OutcomeStatus.OK
        )
        references = dict.fromkeys(
            [
                *REFERENCE.findall(written),
                *REFERENCE.findall(run.answer),
                *(str(p["approval_id"]) for p in run.pending_approvals),
            ]
        )
        return TurnResult(
            thread_id=run.thread_id or "",
            request_id=run.request_id,
            trace_id=run.trace_id,
            answer=run.answer,
            stop_reason=run.stop_reason.value,
            activity=activity,
            citations=[Citation(d, self._titles.get(d, d)) for d in cited if d in self._titles],
            references=list(references),
            pending_approvals=run.pending_approvals,
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
            latency_ms=run.latency_ms,
            model_error=_model_error(new_steps)
            if run.stop_reason is StopReason.MODEL_ERROR
            else None,
        )

    def thread_view(self, user: UserContext, thread_id: str) -> ThreadView | None:
        """None if the thread does not exist. `ThreadAccessError` if it is someone else's."""
        messages = self.agent.history(thread_id, user)
        if not messages:
            return None
        shown = [
            ThreadMessage("user" if isinstance(m, HumanMessage) else "assistant", m.text)
            for m in messages
            if isinstance(m, HumanMessage) or (isinstance(m, AIMessage) and not m.tool_calls)
        ]
        return ThreadView(thread_id, shown, self.agent.pending_approvals(thread_id))

    # -- approvals -------------------------------------------------------------------------

    def decide(
        self, approver: UserContext, approval_id: str, *, approve: bool, comment: str | None
    ) -> Decision:
        """Record the decision; once the request is settled, resume the paused workflow."""
        result = self.approvals.decide(approval_id, approver, approve=approve, comment=comment)
        request = result.request
        if request.status.is_open:
            return Decision(result, note=f"{request.request_id} still waits for other approvals.")
        if request.thread_id is None:
            return Decision(result, note="No conversation to resume.")
        try:
            with self._thread_lock(request.thread_id):
                config: RunnableConfig = {"configurable": {"thread_id": request.thread_id}}
                before = self.agent.graph.get_state(config).values.get("trajectory", [])
                run = self.agent.resume(request.thread_id)
        except NotPausedError:
            return Decision(result, note=f"Thread {request.thread_id} is not paused.")
        return Decision(result, resumed=self.turn_result(run, first_step=len(before)))

    def reconcile(self) -> list[Reconciled]:
        """Finish access requests that were approved but never provisioned (M11).

        Two ways this happens: the process died between the last decision and the
        resume (the thread is still paused), or provisioning failed when it ran (a
        store or MCP server was down). Only requests made through the assistant
        (they have a thread) are touched; provisioning is idempotent and goes
        through the policy gateway like any other call.
        """
        done: list[Reconciled] = []
        for employee_id in self.repository.list_employee_ids():
            for request in self.repository.access_requests_for(employee_id):
                if request.thread_id is None or request.provisioned_at is not None:
                    continue
                if request.status not in PROVISIONABLE:
                    continue
                done.append(self._reconcile_one(request))
        return done

    def _reconcile_one(self, request: AccessRequest) -> Reconciled:
        thread_id = request.thread_id or ""
        with self._thread_lock(thread_id):
            if self._paused(thread_id):
                run = self.agent.resume(thread_id)
                return Reconciled(request.request_id, "resumed", run.answer)
        if self.provisioner is None:
            return Reconciled(request.request_id, "skipped", "no provisioner configured")
        try:
            requester = authenticate(self.repository, request.employee_id)
        except AuthenticationError:
            return Reconciled(request.request_id, "skipped", "requester is not active")
        outcome = self.provisioner.execute(
            PROVISION_TOOL,
            {"access_request_id": request.request_id},
            user=requester,
            request_id=f"reconcile-{request.request_id}",
            thread_id=thread_id,
        )
        if outcome.status is OutcomeStatus.OK:
            return Reconciled(request.request_id, "provisioned", None)
        return Reconciled(request.request_id, "failed", outcome.error_category)

    def _paused(self, thread_id: str) -> bool:
        state = self.agent.graph.get_state({"configurable": {"thread_id": thread_id}})
        return bool(state.interrupts)

    # -- audit -----------------------------------------------------------------------------

    def audit_events(
        self, viewer: UserContext, *, request_id: str | None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Own events only; auditors (security approvers, IT admins) see everyone's."""
        auditor = bool({"security_approver", "it_admin"} & set(viewer.roles))
        events = self.audit.query(
            request_id=request_id,
            user_id=None if auditor else viewer.employee_id,
            limit=limit,
        )
        if not auditor:
            # Approval events are recorded under the requester; decisions by this
            # viewer on others' requests are theirs to see too.
            events = [e for e in events if viewer.employee_id in (e.user_id, e.approver_id)]
        return [json.loads(e.model_dump_json()) for e in events]

    # -- health ----------------------------------------------------------------------------

    def readiness(self) -> Readiness:
        ready = Readiness()
        uses_pg = (
            self.settings.data_store is DataStoreKind.POSTGRES
            or self.settings.audit_store is AuditStoreKind.POSTGRES
            or self.settings.checkpoint_store is CheckpointStoreKind.POSTGRES
            or self.settings.vector_store is VectorStoreKind.PGVECTOR
        )
        if uses_pg:
            try:
                engine = create_engine(self.settings.database_url, pool_pre_ping=True)
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                engine.dispose()
                ready.components["database"] = "ok"
            except Exception as exc:
                ready.components["database"] = f"unavailable ({type(exc).__name__})"
        try:
            self.agent.graph.get_state({"configurable": {"thread_id": "readiness-probe"}})
            ready.components["checkpointer"] = "ok"
        except Exception as exc:
            ready.components["checkpointer"] = f"unavailable ({type(exc).__name__})"
        ready.components["agent"] = "ok"
        ready.circuits = breaker_states()
        return ready


ROUTER_MODEL_ERROR = "model unavailable: "


def _model_error(steps: list[TrajectoryStep]) -> str:
    """The failure category of the model call that ended the turn."""
    for step in reversed(steps):
        if isinstance(step, RouteStep) and (step.error or "").startswith(ROUTER_MODEL_ERROR):
            return (step.error or "")[len(ROUTER_MODEL_ERROR) :]
        if isinstance(step, ModelStep) and step.error in OUTAGE | {"model_error"}:
            return step.error or "model_unavailable"
    return "model_unavailable"


def _document_titles(settings: Settings) -> dict[str, str]:
    titles = {}
    for path in sorted(settings.documents_dir.glob("*.md")):
        meta = load_document(path).metadata
        titles[meta.document_id] = meta.title
    return titles
