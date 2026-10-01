"""The AegisDesk HTTP API (spec section 33, Milestone 10).

A thin adapter: every route authenticates, calls `AegisRuntime` (the same
components the CLI uses), and maps the result to a response model. No agent,
policy or approval logic lives here, so the UI, the CLI and tests exercise the
same backend.

    uv run aegisdesk api serve            # http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import contextvars
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from aegisdesk.api.auth import AuthenticationRequiredError, CurrentUser, Runtime
from aegisdesk.api.schemas import (
    ActivityOut,
    ApprovalOut,
    AuditResponse,
    DecisionRequest,
    DecisionResponse,
    Me,
    MessageRequest,
    MessageResponse,
    Problem,
    ThreadCreated,
    ThreadResponse,
)
from aegisdesk.approvals.service import ApprovalError
from aegisdesk.audit.events import AuditError
from aegisdesk.config import Settings, get_settings
from aegisdesk.graphs.service_desk_graph import ThreadAccessError
from aegisdesk.identity.context import UserContext
from aegisdesk.observability import tracing
from aegisdesk.observability.logging import configure_logging, log_context
from aegisdesk.observability.metrics import instruments
from aegisdesk.observability.setup import configure_telemetry, prometheus_registry
from aegisdesk.persistence.idempotency import Begin, BeginResult, fingerprint
from aegisdesk.reliability.errors import StoreUnavailableError
from aegisdesk.runtime import Activity, AegisRuntime, TurnResult, request_id_for

logger = logging.getLogger(__name__)

API_VERSION = "0.10.0"
PROBLEM_JSON = "application/problem+json"
MODEL_RETRY_AFTER = 10  # seconds: model outages (M11)
STORE_RETRY_AFTER = 5  # seconds: database outages (M11)
EVENT_STREAM = "text/event-stream"

# ApprovalError categories that mean "you may not" rather than "no such thing".
APPROVAL_STATUS = {"not_found": 404, "expired": 409, "already_decided": 409}


def create_app(
    settings: Settings | None = None,
    *,
    runtime: AegisRuntime | None = None,
    configure_observability: bool = True,
) -> FastAPI:
    """`runtime` lets tests inject one; otherwise the lifespan opens it from settings."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if configure_observability:
            configure_logging(fmt=settings.log_format.value, level=settings.log_level)
            configure_telemetry(
                service_name="aegisdesk-api",
                exporter=settings.telemetry_exporter,
                environment=settings.aegis_env.value,
                prometheus=True,
            )
        if runtime is not None:
            app.state.runtime = runtime
            yield
            return
        with AegisRuntime.open(settings) as opened:
            app.state.runtime = opened
            yield

    app = FastAPI(
        title="AegisDesk API",
        version=API_VERSION,
        description=(
            "Enterprise agentic service desk (synthetic data). Authenticate with the "
            "`X-Employee-Id` header set by the gateway (development: a synthetic employee ID)."
        ),
        lifespan=lifespan,
    )
    if runtime is not None:
        app.state.runtime = runtime  # available without running the lifespan
    _install_middleware(app)
    _install_error_handlers(app)
    _install_routes(app)
    return app


# -- cross-cutting ---------------------------------------------------------------------------


def _install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = str(uuid.uuid4())
        started = time.perf_counter()
        attributes = {"http.request.method": request.method}
        with (
            tracing.span(f"{request.method} {request.url.path}", **attributes) as current,
            log_context(request_id=request.state.request_id),
        ):
            try:
                response = await call_next(request)
            except Exception:  # unhandled: answer here so the response is still observed
                logger.exception("unhandled API error")
                response = _problem(request, 500, "Internal error")
            # The route template (/threads/{thread_id}), never the raw path: low
            # cardinality for metrics, and no IDs in span names.
            route = getattr(request.scope.get("route"), "path", "unmatched")
            current.update_name(f"{request.method} {route}")
            current.set_attribute("http.route", route)
            current.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                tracing.mark_error(current, "http_5xx")
        labels = {"method": request.method, "route": route}
        m = instruments()
        m.http_requests.add(1, {**labels, "status_code": str(response.status_code)})
        m.http_latency.record(time.perf_counter() - started, labels)
        response.headers["X-Request-Id"] = request.state.request_id
        return response


def _problem(
    request: Request, status: int, title: str, detail: str | None = None, **extra: Any
) -> JSONResponse:
    body = Problem(
        title=title,
        status=status,
        detail=detail,
        request_id=getattr(request.state, "request_id", None),
        trace_id=tracing.current_trace_id(),
        **extra,
    )
    response = JSONResponse(
        body.model_dump(exclude_none=True), status_code=status, media_type=PROBLEM_JSON
    )
    if body.retry_after is not None:
        response.headers["Retry-After"] = str(body.retry_after)
    return response


def _model_problem(turn: TurnResult) -> Problem:
    """The model failed the turn (M11): 503 while it is an outage, 502 otherwise."""
    outage = turn.model_outage
    return Problem(
        title="Assistant temporarily unavailable" if outage else "Model call failed",
        status=503 if outage else 502,
        detail=turn.answer,
        category=turn.model_error,
        request_id=turn.request_id,
        trace_id=turn.trace_id,
        thread_id=turn.thread_id,
        retry_after=MODEL_RETRY_AFTER if outage else None,
    )


def _problem_response(problem: Problem) -> JSONResponse:
    response = JSONResponse(
        problem.model_dump(exclude_none=True), status_code=problem.status, media_type=PROBLEM_JSON
    )
    if problem.retry_after is not None:
        response.headers["Retry-After"] = str(problem.retry_after)
    return response


def _store_problem(exc: Exception, request_id: str | None) -> Problem:
    store = exc.store if isinstance(exc, StoreUnavailableError) else "audit"
    return Problem(
        title="Service temporarily unavailable",
        status=503,
        detail="A backing system is unavailable. Nothing more was done; try again shortly.",
        category=f"{store}_unavailable",
        request_id=request_id,
        trace_id=tracing.current_trace_id(),
        retry_after=STORE_RETRY_AFTER,
    )


class _Claim:
    """One request's hold on its Idempotency-Key (M11)."""

    def __init__(
        self, runtime: AegisRuntime, user: UserContext, key: str, thread_id: str, text: str
    ) -> None:
        self._store = runtime.idempotency
        self._user = user.employee_id
        self._key = key
        self._fingerprint = fingerprint(thread_id, text)

    def begin(self) -> Begin:
        return self._store.begin(self._user, self._key, self._fingerprint)

    def complete(self, payload: dict[str, Any]) -> None:
        self._store.complete(self._user, self._key, payload)

    def release(self) -> None:
        try:
            self._store.release(self._user, self._key)
        except StoreUnavailableError:  # the record will go stale and be taken over
            logger.warning("could not release idempotency key")


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AuthenticationRequiredError)
    async def not_authenticated(request: Request, exc: AuthenticationRequiredError) -> JSONResponse:
        response = _problem(request, 401, "Not authenticated", str(exc))
        response.headers["WWW-Authenticate"] = "X-Employee-Id"
        return response

    @app.exception_handler(ThreadAccessError)
    async def not_your_thread(request: Request, _exc: ThreadAccessError) -> JSONResponse:
        # 404, not 403: do not confirm that someone else's thread exists.
        return _problem(request, 404, "Thread not found")

    @app.exception_handler(StoreUnavailableError)
    async def store_down(request: Request, exc: StoreUnavailableError) -> JSONResponse:
        logger.warning("%s store unavailable", exc.store)
        return _problem_response(_store_problem(exc, getattr(request.state, "request_id", None)))

    @app.exception_handler(AuditError)
    async def audit_down(request: Request, exc: AuditError) -> JSONResponse:
        # Nothing that must be audited happens without the audit trail (M6).
        logger.warning("audit trail unavailable: %s", type(exc).__name__)
        return _problem_response(_store_problem(exc, getattr(request.state, "request_id", None)))

    @app.exception_handler(ApprovalError)
    async def approval_refused(request: Request, exc: ApprovalError) -> JSONResponse:
        status = APPROVAL_STATUS.get(exc.category, 403)
        title = "Approval not found" if status == 404 else "Approval decision refused"
        return _problem(request, status, title, category=exc.category)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages only; never echo the submitted values.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return _problem(request, 422, "Invalid request", problems)


# -- routes ------------------------------------------------------------------------------


def _install_routes(app: FastAPI) -> None:
    v1 = "/api/v1"

    @app.get("/health", tags=["operations"])
    def health() -> dict[str, str]:
        """Liveness: the process is up and serving."""
        return {"status": "ok"}

    @app.get("/ready", tags=["operations"], responses={503: {"model": Problem}})
    def ready(runtime: Runtime) -> JSONResponse:
        """Readiness: the dependencies a request needs are reachable."""
        report = runtime.readiness()
        status = 200 if report.ready else 503
        body: dict[str, Any] = {
            "status": "ready" if report.ready else "not ready",
            **report.components,
        }
        if report.circuits:  # M11: open circuits mean degraded, not unready
            body["circuits"] = report.circuits
        return JSONResponse(body, status_code=status)

    @app.get("/metrics", tags=["operations"], response_class=Response)
    def metrics() -> Response:
        """Prometheus exposition of the spec section 24 metrics (no user labels)."""
        registry = prometheus_registry()
        payload = generate_latest(registry) if registry is not None else b""
        return Response(payload, media_type=CONTENT_TYPE_LATEST)

    @app.get(f"{v1}/me", tags=["identity"])
    def me(user: CurrentUser, runtime: Runtime) -> Me:
        """Who the API thinks you are (from the gateway header, checked against the directory)."""
        employee = runtime.repository.get_employee(user.employee_id)
        return Me(
            employee_id=user.employee_id,
            name=employee.name if employee else user.employee_id,
            department=user.department,
            roles=list(user.roles),
            manager_id=user.manager_id,
        )

    @app.post(f"{v1}/threads", tags=["threads"], status_code=201)
    def create_thread(user: CurrentUser) -> ThreadCreated:
        """A new conversation. It belongs to the caller from its first message."""
        return ThreadCreated(thread_id=str(uuid.uuid4()))

    @app.post(
        f"{v1}/threads/{{thread_id}}/messages",
        tags=["threads"],
        response_model=MessageResponse,
        responses={
            200: {
                "content": {EVENT_STREAM: {}},
                "description": "JSON, or server-sent events with `Accept: text/event-stream`: "
                "`activity` events as the work progresses, then one `result` event "
                "(the same MessageResponse) or an `error` event (a Problem).",
            }
        },
    )
    def send_message(
        thread_id: str,
        body: MessageRequest,
        request: Request,
        user: CurrentUser,
        runtime: Runtime,
        accept: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[
            str | None,
            Header(
                max_length=128,
                description="A retry with the same key returns the stored response of the "
                "first attempt (header Idempotent-Replayed: true) instead of running again; "
                "409 while the first attempt is still running; 422 if the key was used for "
                "a different message. Writes use the same request ID, so they happen once.",
            ),
        ] = None,
    ) -> Any:
        request_id = request_id_for(user, idempotency_key) or request.state.request_id
        request.state.request_id = request_id
        streaming = accept is not None and EVENT_STREAM in accept
        claim = None
        if idempotency_key:
            claim = _Claim(runtime, user, idempotency_key, thread_id, body.text)
            begun = claim.begin()
            if begun.result is BeginResult.REPLAY and begun.response is not None:
                instruments().idempotency_replays.add(1, {"route": "messages"})
                replayed = {"Idempotent-Replayed": "true"}
                if streaming:
                    return StreamingResponse(
                        iter([_sse("result", begun.response)]),
                        media_type=EVENT_STREAM,
                        headers=replayed,
                    )
                return JSONResponse(begun.response, headers=replayed)
            if begun.result is BeginResult.IN_PROGRESS:
                return _problem(
                    request,
                    409,
                    "Request in progress",
                    "The first attempt with this Idempotency-Key is still running.",
                    category="request_in_progress",
                    retry_after=1,
                )
            if begun.result is BeginResult.MISMATCH:
                return _problem(
                    request,
                    422,
                    "Idempotency-Key reused",
                    "This Idempotency-Key was already used for a different message.",
                    category="idempotency_key_reused",
                )
        if streaming:
            return StreamingResponse(
                _event_stream(runtime, user, thread_id, body.text, request_id, claim),
                media_type=EVENT_STREAM,
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        try:
            turn = runtime.send_message(user, thread_id, body.text, request_id=request_id)
        except BaseException:
            if claim is not None:
                claim.release()  # a failed attempt must not be replayed
            raise
        if turn.model_error is not None:
            if claim is not None:
                claim.release()
            return _problem_response(_model_problem(turn))
        payload = MessageResponse.of(turn).model_dump(mode="json")
        if claim is not None:
            claim.complete(payload)
        return JSONResponse(payload)

    @app.get(f"{v1}/threads/{{thread_id}}", tags=["threads"], responses={404: {"model": Problem}})
    def get_thread(thread_id: str, request: Request, user: CurrentUser, runtime: Runtime) -> Any:
        """The conversation (user and assistant messages only) and what it waits for."""
        view = runtime.thread_view(user, thread_id)
        if view is None:
            return _problem(request, 404, "Thread not found")
        return ThreadResponse.of(view)

    @app.get(f"{v1}/approvals", tags=["approvals"])
    def list_approvals(user: CurrentUser, runtime: Runtime) -> list[ApprovalOut]:
        """Approval steps waiting for the caller's decision."""
        return [
            ApprovalOut.of(a, runtime.repository) for a in runtime.approvals.list_pending_for(user)
        ]

    @app.get(
        f"{v1}/approvals/{{approval_id}}", tags=["approvals"], responses={404: {"model": Problem}}
    )
    def get_approval(approval_id: str, user: CurrentUser, runtime: Runtime) -> ApprovalOut:
        return ApprovalOut.of(runtime.approvals.get(approval_id, user), runtime.repository)

    def decide(
        approval_id: str,
        body: DecisionRequest | None,
        user: Any,
        runtime: AegisRuntime,
        *,
        approve: bool,
    ) -> DecisionResponse:
        decision = runtime.decide(
            user, approval_id, approve=approve, comment=body.comment if body else None
        )
        result = decision.result
        return DecisionResponse(
            approval=ApprovalOut.of(result.approval, runtime.repository),
            request_status=result.request.status.value,
            changed=result.changed,
            resumed=MessageResponse.of(decision.resumed) if decision.resumed else None,
            note=decision.note,
        )

    decision_errors: dict[int | str, dict[str, Any]] = {
        403: {"model": Problem},
        404: {"model": Problem},
        409: {"model": Problem},
    }

    @app.post(
        f"{v1}/approvals/{{approval_id}}/approve", tags=["approvals"], responses=decision_errors
    )
    def approve(
        approval_id: str, user: CurrentUser, runtime: Runtime, body: DecisionRequest | None = None
    ) -> DecisionResponse:
        """Approve one step. When the request is fully decided, the paused workflow resumes."""
        return decide(approval_id, body, user, runtime, approve=True)

    @app.post(
        f"{v1}/approvals/{{approval_id}}/reject", tags=["approvals"], responses=decision_errors
    )
    def reject(
        approval_id: str, user: CurrentUser, runtime: Runtime, body: DecisionRequest | None = None
    ) -> DecisionResponse:
        """Reject one step. The request closes and the paused workflow resumes to say so."""
        return decide(approval_id, body, user, runtime, approve=False)

    @app.get(f"{v1}/audit", tags=["audit"])
    def audit(
        user: CurrentUser,
        runtime: Runtime,
        request_id: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> AuditResponse:
        """Audit events: your own (auditors: everyone's), optionally for one request."""
        return AuditResponse(events=runtime.audit_events(user, request_id=request_id, limit=limit))


# -- streaming ---------------------------------------------------------------------------


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _event_stream(
    runtime: AegisRuntime,
    user: Any,
    thread_id: str,
    text: str,
    request_id: str,
    claim: _Claim | None,
) -> Iterator[str]:
    """Run the turn in a worker thread; relay activity as it happens, then the result.

    The worker runs in a copy of this context, so its spans join the request's trace.
    """
    events: queue.Queue[tuple[str, Any]] = queue.Queue()

    def on_activity(item: Activity) -> None:
        events.put(("activity", ActivityOut(text=item.text, status=item.status).model_dump()))

    def fail(problem: Problem) -> None:
        if claim is not None:
            claim.release()  # a failed attempt must not be replayed
        events.put(("error", problem.model_dump(exclude_none=True)))

    def work() -> None:
        try:
            turn = runtime.send_message(
                user, thread_id, text, request_id=request_id, on_activity=on_activity
            )
            if turn.model_error is not None:
                fail(_model_problem(turn))
                return
            payload = MessageResponse.of(turn).model_dump(mode="json")
            if claim is not None:
                claim.complete(payload)
            events.put(("result", payload))
        except ThreadAccessError:
            fail(Problem(title="Thread not found", status=404))
        except (StoreUnavailableError, AuditError) as exc:
            fail(_store_problem(exc, request_id))
        except Exception:
            logger.exception("streamed turn failed")
            fail(Problem(title="Internal error", status=500, request_id=request_id))
        finally:
            events.put(("end", None))

    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(work,), daemon=True).start()
    while True:
        event, data = events.get()
        if event == "end":
            return
        yield _sse(event, data)
