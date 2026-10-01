"""Milestone 11: reliability building blocks and the agents' behaviour under failure.

Each spec failure (MCP timeout, database error, model timeout, malformed
response, duplicate request, tool failure) is injected at its real seam and the
system must degrade safely: no crash, no unintended write, a clear answer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.exc import IntegrityError, OperationalError

from aegisdesk.agents.loop import (
    EMPTY_ANSWER,
    MODEL_UNAVAILABLE_ANSWER,
    STEP_LIMIT_ANSWER,
    ModelStep,
    RouteStep,
    StopReason,
    ToolStep,
)
from aegisdesk.agents.supervisor import (
    build_supervisor_agent,
    specialist_identity,
    specialist_tools,
)
from aegisdesk.audit.events import InMemoryAuditLog
from aegisdesk.config import Settings, ToolTransport
from aegisdesk.domain.access_store import GuardedAccessStore
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.factory import build_gateway
from aegisdesk.identity.context import UserContext
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.observability import faults
from aegisdesk.persistence.idempotency import BeginResult, InMemoryIdempotencyStore
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.reliability.breaker import BreakerState, CircuitBreaker
from aegisdesk.reliability.errors import StoreUnavailableError, is_database_outage
from aegisdesk.reliability.model_guard import ModelCallError, ModelGuard, classify
from aegisdesk.tools.handoff import AgentName
from aegisdesk.tools.transport import ToolFactory

FAST = {"model_retry_backoff_seconds": 0.0}  # retries without waiting


@pytest.fixture
def inject(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def set_faults(spec: str) -> None:
        monkeypatch.setenv(faults.ENV, spec)
        faults.reload()

    yield set_faults
    monkeypatch.delenv(faults.ENV, raising=False)
    faults.reload()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# -- circuit breaker ----------------------------------------------------------------------


def test_breaker_opens_after_repeated_failures_and_recovers_after_a_trial() -> None:
    clock = Clock()
    circuit = CircuitBreaker("dep", failure_threshold=3, reset_seconds=30, clock=clock)

    for _ in range(3):
        assert circuit.allow()
        circuit.record_failure()
    assert circuit.state is BreakerState.OPEN and not circuit.allow()
    assert circuit.retry_after() == 30

    clock.now += 30
    assert circuit.state.value == "half_open"  # a property that changes with time
    assert circuit.allow() and not circuit.allow()  # one trial at a time
    circuit.record_success()
    assert circuit.state.value == "closed" and circuit.allow()


def test_a_failed_trial_reopens_the_breaker_immediately() -> None:
    clock = Clock()
    circuit = CircuitBreaker("dep", failure_threshold=1, reset_seconds=10, clock=clock)
    circuit.record_failure()
    clock.now += 10
    assert circuit.allow()
    circuit.record_failure()
    assert circuit.state is BreakerState.OPEN and circuit.retry_after() == 10


def test_success_resets_the_failure_count() -> None:
    circuit = CircuitBreaker("dep", failure_threshold=2, reset_seconds=10)
    circuit.record_failure()
    circuit.record_success()
    circuit.record_failure()
    assert circuit.state is BreakerState.CLOSED


# -- model guard --------------------------------------------------------------------------


class _Flaky:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = list(failures)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


class RateLimitError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class _ServerError(Exception):
    status_code = 529


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (TimeoutError(), "model_timeout"),
        (ConnectionError(), "model_unavailable"),
        (RateLimitError(), "model_rate_limited"),
        (_ServerError(), "model_unavailable"),
        (AuthenticationError(), "model_error"),
        (KeyError("bug"), None),
    ],
)
def test_failures_are_classified(exc: BaseException, category: str | None) -> None:
    assert classify(exc) == category


def test_guard_retries_with_backoff_then_succeeds() -> None:
    sleeps: list[float] = []
    guard = ModelGuard(
        "m",
        retries=2,
        backoff_seconds=1.0,
        circuit=CircuitBreaker("m1"),
        sleep=sleeps.append,
        jitter=lambda: 1.0,
    )
    flaky = _Flaky([TimeoutError(), ConnectionError()])

    assert guard.call(flaky, target="router") == "ok"
    assert flaky.calls == 3 and sleeps == [1.0, 2.0]  # exponential


def test_guard_gives_up_with_a_category_and_never_retries_client_errors() -> None:
    guard = ModelGuard("m", retries=2, circuit=CircuitBreaker("m2"), sleep=lambda _: None)

    with pytest.raises(ModelCallError) as exhausted:
        guard.call(_Flaky([TimeoutError()] * 3), target="router")
    assert exhausted.value.category == "model_timeout" and exhausted.value.outage

    auth = _Flaky([AuthenticationError()])
    with pytest.raises(ModelCallError) as refused:
        guard.call(auth, target="router")
    assert refused.value.category == "model_error" and not refused.value.outage
    assert auth.calls == 1


def test_guard_does_not_hide_bugs() -> None:
    guard = ModelGuard("m", retries=2, circuit=CircuitBreaker("m3"), sleep=lambda _: None)
    bug = _Flaky([KeyError("oops")])
    with pytest.raises(KeyError):
        guard.call(bug, target="router")
    assert bug.calls == 1


def test_open_circuit_fails_fast_without_calling_the_model() -> None:
    guard = ModelGuard(
        "m", retries=0, circuit=CircuitBreaker("m4", failure_threshold=2), sleep=lambda _: None
    )
    for _ in range(2):
        with pytest.raises(ModelCallError):
            guard.call(_Flaky([TimeoutError()]), target="router")

    untouched = _Flaky([])
    with pytest.raises(ModelCallError) as fast:
        guard.call(untouched, target="router")
    assert fast.value.category == "circuit_open" and fast.value.retry_after
    assert untouched.calls == 0


def test_injected_model_faults_hit_only_their_target(inject: Any) -> None:
    inject("model_timeout:router")
    guard = ModelGuard("m", circuit=CircuitBreaker("m5"), sleep=lambda _: None)
    with pytest.raises(ModelCallError):
        guard.call(lambda: "ok", target="router")
    assert guard.call(lambda: "ok", target="service_desk") == "ok"


# -- agents under model failure ---------------------------------------------------------


def _agent(
    repository: ServiceDeskRepository,
    retriever: Retriever,
    model: ScriptedChatModel | None = None,
    **settings: Any,
) -> Any:
    return build_supervisor_agent(
        Settings(**{**FAST, **settings}),
        repository,
        checkpointer=InMemorySaver(),
        model=model,
        retriever=retriever,
    )


def test_router_timeout_ends_the_turn_safely(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext, inject: Any
) -> None:
    inject("model_timeout:router")
    before = len(repository.list_tickets_for("E1004"))
    run = _agent(repository, retriever).run("My VPN keeps dropping. Create a ticket.", user=aisha)

    assert run.stop_reason is StopReason.MODEL_ERROR and run.answer == MODEL_UNAVAILABLE_ANSWER
    [route] = [s for s in run.trajectory if isinstance(s, RouteStep)]
    assert route.error == "model unavailable: model_timeout"
    assert run.tool_steps == [] and len(repository.list_tickets_for("E1004")) == before


def test_specialist_timeout_ends_the_turn_safely(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext, inject: Any
) -> None:
    inject("model_unavailable:service_desk")
    run = _agent(repository, retriever).run("Show my tickets", user=aisha)

    assert run.stop_reason is StopReason.MODEL_ERROR and run.answer == MODEL_UNAVAILABLE_ANSWER
    failed = [s for s in run.trajectory if isinstance(s, ModelStep) and s.error]
    assert [s.error for s in failed] == ["model_unavailable"]


def test_breaker_opens_across_requests_and_fails_fast(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext, inject: Any
) -> None:
    inject("model_timeout:router")
    agent = _agent(repository, retriever, breaker_failure_threshold=2, model_max_retries=0)
    for _ in range(2):
        agent.run("Show my tickets", user=aisha)

    third = agent.run("Show my tickets", user=aisha)

    [route] = [s for s in third.trajectory if isinstance(s, RouteStep)]
    assert route.error == "model unavailable: circuit_open"


def test_persistently_malformed_tool_calls_stop_at_the_limit_without_writes(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext, inject: Any
) -> None:
    inject("model_malformed:service_desk")
    run = _agent(repository, retriever).run("Create a ticket: my VPN is broken", user=aisha)

    assert run.answer == STEP_LIMIT_ANSWER
    malformed = [s for s in run.tool_steps if s.error_category == "malformed_tool_call"]
    assert malformed and all(s.status.value == "error" for s in malformed)
    assert [t.ticket_id for t in repository.list_tickets_for("E1004")] == ["INC-1001", "INC-1002"]


def test_a_malformed_tool_call_is_answered_and_the_model_can_correct_it(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    route = {"tasks": [{"agent": "service_desk", "instruction": "ticket"}], "out_of_scope": False}
    ticket = {
        "title": "VPN broken",
        "description": "VPN drops every ten minutes",
        "category": "vpn",
        "priority": "low",
    }
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "RoutingPlan", "args": route, "id": "r"}]),
            AIMessage(
                content="",
                invalid_tool_calls=[
                    {
                        "type": "invalid_tool_call",
                        "name": "create_ticket",
                        "args": '{"title": "VPN',
                        "id": "bad-1",
                        "error": "json",
                    }
                ],
            ),
            AIMessage(
                content="", tool_calls=[{"name": "create_ticket", "args": ticket, "id": "good-1"}]
            ),
            AIMessage(content="Ticket created."),
        ]
    )

    run = _agent(repository, retriever, model=model).run("Create a ticket", user=aisha)

    assert run.answer == "Ticket created."
    categories = [s.error_category for s in run.tool_steps if isinstance(s, ToolStep)]
    assert categories == ["malformed_tool_call", None]
    answered = [
        m
        for call in model.calls
        for m in call
        if isinstance(m, ToolMessage) and m.tool_call_id == "bad-1"
    ]
    assert answered and "not valid JSON" in str(answered[0].content)


def test_an_empty_model_reply_becomes_a_safe_answer(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    route = {
        "tasks": [{"agent": "service_desk", "instruction": "answer the question"}],
        "out_of_scope": False,
    }
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "RoutingPlan", "args": route, "id": "r"}]),
            AIMessage(content="   "),
        ]
    )
    run = _agent(repository, retriever, model=model).run("hello?", user=aisha)
    assert run.answer == EMPTY_ANSWER
    assert [s.error for s in run.trajectory if isinstance(s, ModelStep)] == ["empty_response"]


# -- database errors ----------------------------------------------------------------------


def test_access_store_outage_refuses_the_request_without_writing(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext, inject: Any
) -> None:
    inject("db_error:access")
    before = len(repository.access_store.inner.access_requests_for("E1004"))  # type: ignore[attr-defined]
    run = _agent(repository, retriever).run(
        "Please create an access request for FinanceERP for month-end reporting", user=aisha
    )

    assert run.stop_reason is StopReason.FINAL_ANSWER  # the turn completes and says so
    assert {s.error_category for s in run.tool_steps} == {"unavailable"}
    inject("")
    assert len(repository.access_requests_for("E1004")) == before


def test_audit_outage_refuses_writes_but_not_reads(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext, inject: Any
) -> None:
    inject("db_error:audit")
    agent = _agent(repository, retriever)

    ticket = agent.run("My VPN keeps disconnecting. Create a ticket.", user=aisha)
    tickets = agent.run("Show my tickets", user=aisha)

    [write] = ticket.tool_steps
    assert write.error_category == "policy_denied" and "audit_unavailable" in write.result
    assert tickets.tool_steps[0].status.value == "ok"
    assert [t.ticket_id for t in repository.list_tickets_for("E1004")] == ["INC-1001", "INC-1002"]


def test_checkpoint_outage_is_a_store_unavailable_error(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext, inject: Any
) -> None:
    inject("db_error:checkpoint")
    with pytest.raises(StoreUnavailableError) as down:
        _agent(repository, retriever).run("Show my tickets", user=aisha)
    assert down.value.store == "checkpoint"


def test_driver_outages_become_store_unavailable_and_bugs_do_not() -> None:
    class Failing:
        def access_for(self, employee_id: str) -> list[Any]:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        def get_access_request(self, request_id: str) -> None:
            raise IntegrityError("INSERT", {}, Exception("duplicate"))

    store = GuardedAccessStore(Failing())  # type: ignore[arg-type]
    with pytest.raises(StoreUnavailableError):
        store.access_for("E1004")
    with pytest.raises(IntegrityError):
        store.get_access_request("AR-1")
    assert is_database_outage(sqlite3.OperationalError("locked"))
    assert not is_database_outage(RuntimeError("OperationalError"))


# -- MCP outage + breaker ----------------------------------------------------------------


def test_mcp_breaker_fails_fast_once_the_server_keeps_failing(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext, inject: Any
) -> None:
    settings = Settings(tool_transport=ToolTransport.MCP_INPROCESS, breaker_failure_threshold=2)
    gateway = build_gateway(
        settings, audit=InMemoryAuditLog(), access_store=repository.access_store
    )
    ticket = {
        "title": "VPN broken",
        "description": "VPN drops every ten minutes",
        "category": "vpn",
        "priority": "low",
    }
    with ToolFactory.from_settings(settings, repository, gateway=gateway) as factory:
        runner = factory.runner(
            specialist_identity(AgentName.SERVICE_DESK, settings),
            specialist_tools(repository, retriever)[AgentName.SERVICE_DESK],
        )
        inject("mcp_unavailable:action")
        first = [
            runner.execute("create_ticket", ticket, user=aisha, request_id=f"r{i}")
            for i in range(2)
        ]
        inject("")  # the server is back, but the circuit is still open
        third = runner.execute("create_ticket", ticket, user=aisha, request_id="r3")

    assert [o.error_category for o in first] == ["unavailable", "unavailable"]
    assert third.error_category == "unavailable" and "temporarily unavailable" in third.content
    assert [t.ticket_id for t in repository.list_tickets_for("E1004")] == ["INC-1001", "INC-1002"]


# -- idempotency store ------------------------------------------------------------------


def test_idempotency_store_semantics() -> None:
    now = [datetime(2026, 9, 30, tzinfo=UTC)]
    store = InMemoryIdempotencyStore(
        ttl=timedelta(hours=24), stale_after=timedelta(minutes=5), clock=lambda: now[0]
    )

    assert store.begin("E1004", "k", "fp").result is BeginResult.NEW
    assert store.begin("E1004", "k", "fp").result is BeginResult.IN_PROGRESS
    assert store.begin("E1004", "k", "other").result is BeginResult.MISMATCH
    assert store.begin("E1001", "k", "fp").result is BeginResult.NEW  # keys are per user

    store.complete("E1004", "k", {"answer": "done"})
    replay = store.begin("E1004", "k", "fp")
    assert (replay.result, replay.response) == (BeginResult.REPLAY, {"answer": "done"})

    now[0] += timedelta(hours=25)  # expired: the key may be reused
    assert store.begin("E1004", "k", "new-fp").result is BeginResult.NEW


def test_failed_attempts_release_and_stale_attempts_are_taken_over() -> None:
    now = [datetime(2026, 9, 30, tzinfo=UTC)]
    store = InMemoryIdempotencyStore(stale_after=timedelta(minutes=5), clock=lambda: now[0])

    store.begin("E1004", "k", "fp")
    store.release("E1004", "k")
    assert store.begin("E1004", "k", "fp").result is BeginResult.NEW

    now[0] += timedelta(minutes=6)  # the worker died without completing or releasing
    assert store.begin("E1004", "k", "fp").result is BeginResult.NEW
