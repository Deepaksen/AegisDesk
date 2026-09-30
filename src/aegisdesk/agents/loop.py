"""A hand-written tool-calling agent loop.

This is the whole idea of an "agent" with nothing hidden:

    messages = [system, *history, user]
    repeat (at most max_steps times):
        reply = model(messages, tools)             # the model decides
        if reply has no tool calls:  return reply  # final answer
        for each requested call:                   # the application acts
            result = executor.execute(call, trusted user)
            messages += tool result
    stop: step limit reached

The model chooses *which* tool to call and with *what* arguments. The
application decides *whether* anything runs, as whom, how often, and when to
stop. Milestone 2 rebuilds this loop as a LangGraph graph; keeping this
version makes it clear what LangGraph adds (explicit state, checkpointing,
interrupts, streaming) and what it does not change.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import ToolCall

from aegisdesk.identity.context import UserContext
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.prompts.loader import Prompt
from aegisdesk.tools.executor import OutcomeStatus, ToolRunner

STEP_LIMIT_ANSWER = (
    "I couldn't finish this request within the allowed number of steps. "
    "Please try rephrasing it, or contact the IT service desk directly."
)
TOOL_LIMIT_ANSWER = (
    "This request needed more actions than I'm allowed to take at once. "
    "Please split it into smaller requests, or contact the IT service desk directly."
)


class StopReason(StrEnum):
    FINAL_ANSWER = "final_answer"
    MAX_STEPS = "max_steps"
    MAX_TOOL_CALLS = "max_tool_calls"


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 6  # model calls per request
    max_tool_calls: int = 8  # tool executions per request

    def __post_init__(self) -> None:
        if self.max_steps < 1 or self.max_tool_calls < 0:
            raise ValueError("max_steps must be >= 1 and max_tool_calls >= 0")


@dataclass(frozen=True)
class ModelStep:
    step: int
    usage: TokenUsage
    latency_ms: float
    requested_tools: tuple[str, ...]
    agent: str | None = None  # which specialist made the call (multi-agent runs)


@dataclass(frozen=True)
class ToolStep:
    step: int
    tool_name: str
    args: dict[str, Any]
    status: OutcomeStatus
    latency_ms: float
    error_category: str | None
    result: str
    agent: str | None = None


@dataclass(frozen=True)
class RouteStep:
    """The supervisor's routing decision (multi-agent runs)."""

    tasks: tuple[tuple[str, str], ...]  # (agent, instruction)
    out_of_scope: bool
    error: str | None
    usage: TokenUsage
    latency_ms: float


@dataclass(frozen=True)
class AgentStep:
    """One specialist's piece of work, as the supervisor sees it (multi-agent runs)."""

    agent: str
    instruction: str
    status: str  # done | incomplete | failed
    answer: str
    note: str | None = None


TrajectoryStep = ModelStep | ToolStep | RouteStep | AgentStep


@dataclass(frozen=True)
class AgentRun:
    """Everything that happened in one request: the seed of traces and trajectory evals."""

    agent_name: str
    agent_version: str
    prompt_name: str
    prompt_version: str
    request_id: str
    answer: str
    stop_reason: StopReason
    trajectory: list[TrajectoryStep]
    # The conversation without the system prompt, including this turn;
    # pass it back as `history` for the next turn.
    history: list[BaseMessage]
    latency_ms: float
    usage: TokenUsage = field(default_factory=TokenUsage)
    # Set when the run belongs to a persisted conversation (the LangGraph engine).
    thread_id: str | None = None
    # Approval steps the thread is paused on (Milestone 7); empty when not paused.
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    # The OpenTelemetry trace of this run (Milestone 8): joins logs, audit events, spans.
    trace_id: str | None = None

    @property
    def awaiting_approval(self) -> bool:
        return bool(self.pending_approvals)

    @property
    def llm_calls(self) -> int:
        return sum(isinstance(s, ModelStep | RouteStep) for s in self.trajectory)

    @property
    def tool_steps(self) -> list[ToolStep]:
        return [s for s in self.trajectory if isinstance(s, ToolStep)]


class ToolCallingAgent:
    def __init__(
        self,
        *,
        name: str,
        version: str,
        model: BaseChatModel,
        prompt: Prompt,
        executor: ToolRunner,
        limits: AgentLimits,
    ) -> None:
        self.name = name
        self.version = version
        self._prompt = prompt
        self._executor = executor
        self._limits = limits
        # The model only ever learns about the tools in this executor.
        self._model = model.bind_tools(executor.model_definitions())

    def run(
        self,
        user_input: str,
        *,
        user: UserContext,
        history: list[BaseMessage] | None = None,
        request_id: str | None = None,
    ) -> AgentRun:
        request_id = request_id or str(uuid.uuid4())
        started = time.perf_counter()
        messages: list[BaseMessage] = [
            SystemMessage(content=self._prompt.system),
            *(history or []),
            HumanMessage(content=user_input),
        ]
        trajectory: list[TrajectoryStep] = []
        usage = TokenUsage()
        tool_calls_made = 0

        def finish(answer: str, reason: StopReason) -> AgentRun:
            if reason is not StopReason.FINAL_ANSWER:
                # Keep the history well-formed for the next turn.
                messages.append(AIMessage(content=answer))
            return AgentRun(
                agent_name=self.name,
                agent_version=self.version,
                prompt_name=self._prompt.name,
                prompt_version=self._prompt.version,
                request_id=request_id,
                answer=answer,
                stop_reason=reason,
                trajectory=trajectory,
                history=messages[1:],
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                usage=usage,
            )

        for step in range(1, self._limits.max_steps + 1):
            call_started = time.perf_counter()
            reply = self._model.invoke(messages)
            call_usage = TokenUsage.from_message(reply)
            usage = usage + call_usage
            messages.append(reply)
            trajectory.append(
                ModelStep(
                    step=step,
                    usage=call_usage,
                    latency_ms=round((time.perf_counter() - call_started) * 1000, 2),
                    requested_tools=tuple(c["name"] for c in reply.tool_calls),
                )
            )

            if not reply.tool_calls:
                return finish(reply.text, StopReason.FINAL_ANSWER)

            for index, call in enumerate(reply.tool_calls):
                if tool_calls_made >= self._limits.max_tool_calls:
                    _answer_skipped_calls(messages, reply.tool_calls[index:])
                    return finish(TOOL_LIMIT_ANSWER, StopReason.MAX_TOOL_CALLS)

                tool_calls_made += 1
                outcome = self._executor.execute(
                    call["name"], call["args"], user=user, request_id=request_id
                )
                messages.append(
                    ToolMessage(
                        content=outcome.content,
                        tool_call_id=call["id"] or "",
                        name=call["name"],
                        status="success" if outcome.status is OutcomeStatus.OK else "error",
                    )
                )
                trajectory.append(
                    ToolStep(
                        step=step,
                        tool_name=call["name"],
                        args=dict(call["args"]),
                        status=outcome.status,
                        latency_ms=outcome.latency_ms,
                        error_category=outcome.error_category,
                        result=outcome.content,
                    )
                )

        return finish(STEP_LIMIT_ANSWER, StopReason.MAX_STEPS)


def _answer_skipped_calls(messages: list[BaseMessage], calls: list[ToolCall]) -> None:
    """Providers require every tool call to have a result, even ones we refuse to run."""
    for call in calls:
        messages.append(
            ToolMessage(
                content='{"error": {"category": "not_executed", '
                '"message": "Tool call limit reached."}}',
                tool_call_id=call["id"] or "",
                name=call["name"],
                status="error",
            )
        )
