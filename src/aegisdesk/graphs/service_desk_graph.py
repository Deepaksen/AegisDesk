"""The Service Desk agent as an explicit LangGraph graph.

Same behaviour as the hand-written loop in `aegisdesk.agents.loop`, but every
step is a named **node**, every "what next?" decision is a **conditional
edge**, and the state is checkpointed after each node:

    START → start_turn → call_model ─┬─ no tool calls ─────────────► END
                             ▲       └─ tool calls ─► run_tools ─┐
                             │                                   │
                             └──────── under the limits ◄────────┤
                                                                 └─ limit hit ─► limit_reached → END

What stays ours (unchanged from M1): the tools, the `ToolExecutor` security
boundary, trusted identity and the limits. What LangGraph now provides:
state merging, checkpointing per thread, step-by-step streaming, and a
framework-level recursion limit as a second backstop.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from aegisdesk.agents.loop import (
    EMPTY_ANSWER,
    MODEL_UNAVAILABLE_ANSWER,
    STEP_LIMIT_ANSWER,
    TOOL_LIMIT_ANSWER,
    AgentLimits,
    AgentRun,
    AgentStep,
    ModelStep,
    RouteStep,
    StopReason,
    ToolStep,
    TrajectoryStep,
)
from aegisdesk.graphs.state import ServiceDeskState, claims_from, context_from
from aegisdesk.identity.context import UserContext
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.observability import faults, langsmith, tracing
from aegisdesk.observability.logging import log_context
from aegisdesk.observability.metrics import instruments
from aegisdesk.observability.redaction import pseudonym
from aegisdesk.prompts.loader import Prompt
from aegisdesk.reliability.errors import StoreUnavailableError, is_database_outage
from aegisdesk.reliability.model_guard import ModelCallError, ModelGuard, guard_for
from aegisdesk.tools.executor import OutcomeStatus, ToolRunner, refused_call

NODE_START = "start_turn"
NODE_MODEL = "call_model"
NODE_TOOLS = "run_tools"
NODE_LIMIT = "limit_reached"

# Called with (node name, the partial state update that node returned).
UpdateCallback = Callable[[str, dict[str, Any]], None]


class NotPausedError(RuntimeError):
    """Resume was requested for a thread that is not waiting for anything."""


class ThreadAccessError(PermissionError):
    """The thread exists but belongs to another employee."""


def build_tool_agent_graph(
    *,
    model: BaseChatModel,
    prompt: Prompt,
    executor: ToolRunner,
    limits: AgentLimits,
    checkpointer: BaseCheckpointSaver[Any] | bool | None,
    guard: ModelGuard | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """A tool-calling agent as a graph. Used standalone (M2) and as a specialist subgraph (M4).

    `guard` (M11) wraps every model call: retries, circuit breaker, failure categories.

    `checkpointer=False` compiles a subgraph that never checkpoints on its own,
    not even by inheriting its parent's checkpointer: the parent graph persists
    only what the specialist *returns*, never its private scratch messages.
    """
    model_with_tools = model.bind_tools(executor.model_definitions())
    guard = guard or guard_for(model)

    # -- nodes: plain functions from state to a partial state update ----------

    def start_turn(state: ServiceDeskState) -> dict[str, Any]:
        return {
            "owner_id": state.get("owner_id") or state["user"]["employee_id"],
            "llm_calls": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "stop_reason": None,
            "trajectory": [],
        }

    def call_model(state: ServiceDeskState) -> dict[str, Any]:
        # The system prompt is added here, at call time, not stored in the thread.
        messages = [SystemMessage(content=prompt.system), *state["messages"]]
        started = time.perf_counter()
        model_attrs = tracing.model_attributes(model)
        with tracing.span(
            f"chat {model_attrs.get(tracing.MODEL) or ''}".strip(),
            **{tracing.OPERATION: "chat", tracing.PROMPT: f"{prompt.name}@{prompt.version}"},
            **model_attrs,
        ) as llm_span:
            try:
                reply = guard.call(
                    lambda: _invoke(model_with_tools, messages, prompt.name), target=prompt.name
                )
            except ModelCallError as exc:
                tracing.mark_error(llm_span, exc.category)
                return _model_failed(state, exc, started)
            usage = TokenUsage.from_message(reply)
            tracing.record_llm_call(
                llm_span,
                model=model_attrs,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                seconds=time.perf_counter() - started,
                agent=prompt.name,
            )
            llm_span.set_attribute(
                "aegisdesk.requested_tools", [c["name"] for c in reply.tool_calls]
            )
        step = state["llm_calls"] + 1
        error = None
        if reply.invalid_tool_calls:
            error = "malformed_tool_call"  # answered in run_tools, so the model can correct it
        elif not reply.tool_calls and not reply.text.strip():
            error = "empty_response"
            reply = AIMessage(content=EMPTY_ANSWER)  # never an empty answer to the user
        entry = {
            "kind": "model",
            "step": step,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "requested_tools": [c["name"] for c in reply.tool_calls],
            "error": error,
        }
        return {
            "messages": [reply],
            "llm_calls": step,
            "input_tokens": state["input_tokens"] + usage.input_tokens,
            "output_tokens": state["output_tokens"] + usage.output_tokens,
            "trajectory": [*state["trajectory"], entry],
            "stop_reason": (
                None
                if reply.tool_calls or reply.invalid_tool_calls
                else StopReason.FINAL_ANSWER.value
            ),
        }

    def run_tools(state: ServiceDeskState, config: RunnableConfig) -> dict[str, Any]:
        reply = state["messages"][-1]
        if not isinstance(reply, AIMessage):  # the routing below guarantees this
            raise RuntimeError("run_tools reached without a model reply")
        user = context_from(state["user"])
        made = state["tool_calls"]
        # Specialist subgraphs inherit the parent's config, so this is the
        # conversation thread in both engines (recorded in audit events).
        thread_id = (config.get("configurable") or {}).get("thread_id")
        new_messages: list[BaseMessage] = []
        trajectory = list(state["trajectory"])
        stop_reason: str | None = None

        for invalid in reply.invalid_tool_calls:
            # Malformed arguments (not JSON): answer it like any refused call, so the
            # model sees what went wrong and every tool call id gets a result.
            made += 1
            name = invalid.get("name") or "unknown"
            content = refused_call(
                name,
                "malformed_tool_call",
                "The tool call arguments were not valid JSON. Send the call again with "
                "valid arguments.",
            ).content
            new_messages.append(
                ToolMessage(
                    content=content,
                    tool_call_id=invalid.get("id") or "",
                    name=name,
                    status="error",
                )
            )
            trajectory.append(
                {
                    "kind": "tool",
                    "step": state["llm_calls"],
                    "tool_name": name,
                    "args": {},
                    "status": OutcomeStatus.ERROR.value,
                    "latency_ms": 0.0,
                    "error_category": "malformed_tool_call",
                    "result": content,
                }
            )

        for call in reply.tool_calls:
            if made >= limits.max_tool_calls:
                # Every tool call needs a result, even the ones we refuse to run.
                new_messages.append(_not_executed(call["id"] or "", call["name"]))
                stop_reason = StopReason.MAX_TOOL_CALLS.value
                continue
            made += 1
            outcome = executor.execute(
                call["name"],
                call["args"],
                user=user,
                request_id=state["request_id"],
                thread_id=thread_id,
            )
            new_messages.append(
                ToolMessage(
                    content=outcome.content,
                    tool_call_id=call["id"] or "",
                    name=call["name"],
                    status="success" if outcome.status is OutcomeStatus.OK else "error",
                )
            )
            trajectory.append(
                {
                    "kind": "tool",
                    "step": state["llm_calls"],
                    "tool_name": call["name"],
                    "args": dict(call["args"]),
                    "status": outcome.status.value,
                    "latency_ms": outcome.latency_ms,
                    "error_category": outcome.error_category,
                    "result": outcome.content,
                }
            )

        return {
            "messages": new_messages,
            "tool_calls": made,
            "trajectory": trajectory,
            "stop_reason": stop_reason,
        }

    def limit_reached(state: ServiceDeskState) -> dict[str, Any]:
        reason = StopReason(state.get("stop_reason") or StopReason.MAX_STEPS.value)
        answer = TOOL_LIMIT_ANSWER if reason is StopReason.MAX_TOOL_CALLS else STEP_LIMIT_ANSWER
        return {"messages": [AIMessage(content=answer)], "stop_reason": reason.value}

    # -- conditional edges: plain functions from state to the next node's name --

    def after_model(state: ServiceDeskState) -> str:
        last = state["messages"][-1]
        wants_tools = isinstance(last, AIMessage) and (last.tool_calls or last.invalid_tool_calls)
        return NODE_TOOLS if wants_tools else END

    def after_tools(state: ServiceDeskState) -> str:
        if state.get("stop_reason") == StopReason.MAX_TOOL_CALLS.value:
            return NODE_LIMIT
        if state["llm_calls"] >= limits.max_steps:
            return NODE_LIMIT
        return NODE_MODEL

    graph = StateGraph(ServiceDeskState)
    graph.add_node(NODE_START, start_turn)
    graph.add_node(NODE_MODEL, call_model)
    graph.add_node(NODE_TOOLS, run_tools)
    graph.add_node(NODE_LIMIT, limit_reached)

    graph.add_edge(START, NODE_START)
    graph.add_edge(NODE_START, NODE_MODEL)
    graph.add_conditional_edges(NODE_MODEL, after_model, [NODE_TOOLS, END])
    graph.add_conditional_edges(NODE_TOOLS, after_tools, [NODE_MODEL, NODE_LIMIT])
    graph.add_edge(NODE_LIMIT, END)

    return graph.compile(checkpointer=checkpointer)


@contextmanager
def _checkpoint_errors() -> Iterator[None]:
    try:
        yield
    except StoreUnavailableError:
        raise
    except Exception as exc:
        if is_database_outage(exc):
            raise StoreUnavailableError("checkpoint") from exc
        raise


def _invoke(model: Any, messages: list[BaseMessage], target: str) -> AIMessage:
    if faults.active("model_malformed", target):
        # What a provider returns when the model emits broken tool-call JSON.
        return AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "type": "invalid_tool_call",
                    "name": "create_ticket",
                    "args": '{"title": "VPN',
                    "id": f"malformed-{uuid.uuid4().hex[:8]}",
                    "error": "injected malformed tool call",
                }
            ],
        )
    reply = model.invoke(messages)
    if not isinstance(reply, AIMessage):
        raise TypeError(f"model returned {type(reply).__name__}, not AIMessage")
    return reply


def _model_failed(state: ServiceDeskState, exc: ModelCallError, started: float) -> dict[str, Any]:
    """The model service failed after retries: end the turn with a safe answer."""
    step = state["llm_calls"] + 1
    entry = {
        "kind": "model",
        "step": step,
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "requested_tools": [],
        "error": exc.category,
    }
    return {
        "messages": [AIMessage(content=MODEL_UNAVAILABLE_ANSWER)],
        "llm_calls": step,
        "trajectory": [*state["trajectory"], entry],
        "stop_reason": StopReason.MODEL_ERROR.value,
    }


def _not_executed(call_id: str, name: str) -> ToolMessage:
    return ToolMessage(
        content='{"error": {"category": "not_executed", "message": "Tool call limit reached."}}',
        tool_call_id=call_id,
        name=name,
        status="error",
    )


# The M2 name, kept for readers following the milestones in order.
build_service_desk_graph = build_tool_agent_graph


class ThreadedGraphAgent:
    """Runs turns of a persisted conversation (a *thread*) through a compiled graph.

    Works for any graph whose state has the shared keys of `ServiceDeskState`
    (messages, owner_id, user, request_id, counters, stop_reason, trajectory):
    the single Service Desk agent (M2) and the multi-agent supervisor (M4).
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        prompt: Prompt,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        recursion_limit: int,
    ) -> None:
        self.name = name
        self.version = version
        self._prompt = prompt
        self.graph = graph
        # Our own limits stop the run first; this is LangGraph's backstop.
        self._recursion_limit = recursion_limit

    def _config(self, thread_id: str) -> RunnableConfig:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self._recursion_limit,
        }

    def history(self, thread_id: str, user: UserContext) -> list[BaseMessage]:
        """The stored conversation, if `user` owns the thread (empty if it doesn't exist)."""
        values = self._authorised_state(thread_id, user)
        return list(values.get("messages", []))

    def _authorised_state(self, thread_id: str, user: UserContext) -> dict[str, Any]:
        # Ownership is checked by application code *before* anything is written
        # to the thread, so a stranger cannot even append a message to it.
        values: dict[str, Any] = self._state(thread_id).values
        owner = values.get("owner_id")
        if owner is not None and owner != user.employee_id:
            raise ThreadAccessError(f"Thread {thread_id!r} belongs to another employee.")
        return values

    def _state(self, thread_id: str) -> Any:
        """The thread's checkpoint; a database outage becomes StoreUnavailableError (M11)."""
        if faults.active("db_error", "checkpoint"):
            raise StoreUnavailableError("checkpoint")
        with _checkpoint_errors():
            return self.graph.get_state(self._config(thread_id))

    def run(
        self,
        user_input: str,
        *,
        user: UserContext,
        thread_id: str | None = None,
        request_id: str | None = None,
        on_update: UpdateCallback | None = None,
    ) -> AgentRun:
        thread_id = thread_id or str(uuid.uuid4())
        request_id = request_id or str(uuid.uuid4())
        self._authorised_state(thread_id, user)

        started = time.perf_counter()
        turn_input: dict[str, Any] = {
            "messages": [HumanMessage(content=user_input)],
            "user": claims_from(user),
            "request_id": request_id,
        }
        with self._observed(
            "aegisdesk.request", request_id=request_id, thread_id=thread_id, user=user
        ) as observe:
            before = len(self._state(thread_id).values.get("messages", []))
            with _checkpoint_errors():
                for node, update in self._stream(turn_input, thread_id, request_id):
                    if on_update is not None:
                        on_update(node, update)
            return observe(self._result(thread_id, request_id, started, first_new=before + 1))

    def resume(self, thread_id: str, *, on_update: UpdateCallback | None = None) -> AgentRun:
        """Continue a thread paused for approval (Milestone 7).

        Called by the approval service after a human decision, not by the thread's
        owner, so there is no ownership check: what may happen next is decided by
        the approval records, which the resumed nodes read themselves. No model is
        called on this path.
        """
        snapshot = self._state(thread_id)
        if not snapshot.interrupts:
            raise NotPausedError(f"Thread {thread_id!r} is not waiting for approval.")
        started = time.perf_counter()
        before = len(snapshot.values.get("messages", []))
        # The value is informational only; the nodes re-read the approval store.
        command: Command[Any] = Command(resume={"event": "approval_decided"})
        request_id = str(snapshot.values.get("request_id", ""))
        with self._observed(
            "aegisdesk.resume", request_id=request_id, thread_id=thread_id, user=None
        ) as observe:
            config = self._run_config(thread_id, request_id)
            with _checkpoint_errors():
                for chunk in self.graph.stream(command, config, stream_mode="updates"):
                    for node, update in chunk.items():
                        if on_update is not None and node != "__interrupt__":
                            on_update(node, update or {})
            return observe(self._result(thread_id, request_id, started, first_new=before))

    @contextmanager
    def _observed(
        self, name: str, *, request_id: str, thread_id: str, user: UserContext | None
    ) -> Iterator[Callable[[AgentRun], AgentRun]]:
        """Root span + request metrics + log context for one run (Milestone 8)."""
        labels = {"agent": self.name, "operation": name.rsplit(".", 1)[-1]}
        attributes = {
            tracing.REQUEST_ID: request_id,
            tracing.THREAD_ID: thread_id,
            tracing.AGENT_NAME: self.name,
            tracing.AGENT_VERSION: self.version,
            tracing.PROMPT: f"{self._prompt.name}@{self._prompt.version}",
            tracing.USER: pseudonym(user.employee_id) if user else None,
        }
        started = time.perf_counter()
        m = instruments()
        with (
            tracing.span(name, **attributes) as root,
            log_context(request_id=request_id, thread_id=thread_id, agent=self.name),
        ):

            def finish(run: AgentRun) -> AgentRun:
                root.set_attribute(tracing.GRAPH_STEPS, len(run.trajectory))
                root.set_attribute(tracing.STATUS, run.stop_reason.value)
                if run.pending_approvals:
                    root.set_attribute(
                        "aegisdesk.approval.pending",
                        [p["approval_id"] for p in run.pending_approvals],
                    )
                return dataclasses.replace(run, trace_id=tracing.current_trace_id())

            try:
                yield finish
            except Exception:
                m.requests_failed.add(1, labels)
                raise
            finally:
                m.requests.add(1, labels)
                m.task_latency.record(time.perf_counter() - started, labels)

    def _run_config(self, thread_id: str, request_id: str) -> RunnableConfig:
        """Config for a run: LangSmith metadata and (optional) tracer callbacks."""
        config = self._config(thread_id)
        config["metadata"] = {
            "agent": self.name,
            "agent_version": self.version,
            "prompt": f"{self._prompt.name}@{self._prompt.version}",
            "request_id": request_id,
            "thread_id": thread_id,
        }
        config["tags"] = [f"agent:{self.name}", f"agent_version:{self.version}"]
        config["callbacks"] = langsmith.callbacks()
        return config

    def pending_approvals(self, thread_id: str) -> list[dict[str, Any]]:
        snapshot = self.graph.get_state(self._config(thread_id))
        return [
            item
            for pending in snapshot.interrupts
            if isinstance(pending.value, dict)
            for item in pending.value.get("pending", [])
        ]

    def _result(
        self, thread_id: str, request_id: str, started: float, *, first_new: int
    ) -> AgentRun:
        state: dict[str, Any] = self.graph.get_state(self._config(thread_id)).values
        messages = list(state["messages"])
        # Everything the assistant said in this turn (an answer, then maybe an approval update).
        said = [
            m.text for m in messages[first_new:] if isinstance(m, AIMessage) and not m.tool_calls
        ]
        return AgentRun(
            agent_name=self.name,
            agent_version=self.version,
            prompt_name=self._prompt.name,
            prompt_version=self._prompt.version,
            request_id=request_id,
            answer="\n\n".join(said) if said else messages[-1].text,
            stop_reason=StopReason(state["stop_reason"]),
            trajectory=[step_from_entry(entry) for entry in state["trajectory"]],
            history=messages,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            usage=TokenUsage(state["input_tokens"], state["output_tokens"]),
            thread_id=thread_id,
            pending_approvals=self.pending_approvals(thread_id),
        )

    def _stream(
        self, turn_input: dict[str, Any], thread_id: str, request_id: str
    ) -> Iterator[tuple[str, Any]]:
        # stream_mode="updates" yields {node_name: update} after each node finishes.
        config = self._run_config(thread_id, request_id)
        for chunk in self.graph.stream(turn_input, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":  # a pause, reported via pending_approvals
                    continue
                yield node, update or {}


class ServiceDeskGraphAgent(ThreadedGraphAgent):
    """The single Service Desk agent (Milestone 2) with persisted threads."""

    def __init__(
        self,
        *,
        name: str,
        version: str,
        model: BaseChatModel,
        prompt: Prompt,
        executor: ToolRunner,
        limits: AgentLimits,
        checkpointer: BaseCheckpointSaver[Any],
        guard: ModelGuard | None = None,
    ) -> None:
        super().__init__(
            name=name,
            version=version,
            prompt=prompt,
            graph=build_tool_agent_graph(
                model=model,
                prompt=prompt,
                executor=executor,
                limits=limits,
                checkpointer=checkpointer,
                guard=guard,
            ),
            # Each model call and each tool round is one superstep, plus start/limit.
            recursion_limit=2 * limits.max_steps + 4,
        )


def step_from_entry(entry: dict[str, Any]) -> TrajectoryStep:
    agent = entry.get("agent")
    match entry["kind"]:
        case "model":
            return ModelStep(
                step=entry["step"],
                usage=TokenUsage(entry["input_tokens"], entry["output_tokens"]),
                latency_ms=entry["latency_ms"],
                requested_tools=tuple(entry["requested_tools"]),
                agent=agent,
                error=entry.get("error"),
            )
        case "tool":
            return ToolStep(
                step=entry["step"],
                tool_name=entry["tool_name"],
                args=entry["args"],
                status=OutcomeStatus(entry["status"]),
                latency_ms=entry["latency_ms"],
                error_category=entry["error_category"],
                result=entry["result"],
                agent=agent,
            )
        case "route":
            return RouteStep(
                tasks=tuple((t["agent"], t["instruction"]) for t in entry["tasks"]),
                out_of_scope=entry["out_of_scope"],
                error=entry.get("error"),
                usage=TokenUsage(entry["input_tokens"], entry["output_tokens"]),
                latency_ms=entry["latency_ms"],
            )
        case _:
            return AgentStep(
                agent=entry["agent"],
                instruction=entry["instruction"],
                status=entry["status"],
                answer=entry["answer"],
                note=entry.get("note"),
            )
