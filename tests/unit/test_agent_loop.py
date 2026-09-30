"""The agent loop, driven by a scripted model so every step is predictable."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata

from aegisdesk.agents.loop import (
    STEP_LIMIT_ANSWER,
    TOOL_LIMIT_ANSWER,
    AgentLimits,
    ModelStep,
    StopReason,
    ToolCallingAgent,
    ToolStep,
)
from aegisdesk.config import PROJECT_ROOT
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.prompts.loader import Prompt, load_prompt
from aegisdesk.tools.executor import OutcomeStatus, ToolExecutor
from aegisdesk.tools.service_desk import build_service_desk_tools


def call(name: str, args: dict[str, Any] | None = None, call_id: str = "c1") -> dict[str, Any]:
    return {"name": name, "args": args or {}, "id": call_id}


def ai(text: str = "", *calls: dict[str, Any], tokens: tuple[int, int] = (10, 2)) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=list(calls),
        usage_metadata=UsageMetadata(
            input_tokens=tokens[0], output_tokens=tokens[1], total_tokens=sum(tokens)
        ),
    )


@pytest.fixture
def prompt() -> Prompt:
    return load_prompt(PROJECT_ROOT / "prompts", "service_desk", "v1")


def make_agent(
    model: ScriptedChatModel,
    repository: ServiceDeskRepository,
    prompt: Prompt,
    limits: AgentLimits | None = None,
) -> ToolCallingAgent:
    return ToolCallingAgent(
        name="service_desk",
        version="test",
        model=model,
        prompt=prompt,
        executor=ToolExecutor(build_service_desk_tools(repository)),
        limits=limits or AgentLimits(),
    )


def test_answer_without_tools_ends_after_one_model_call(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("Hi! How can I help?")])

    run = make_agent(model, repository, prompt).run("hello", user=aisha)

    assert run.answer == "Hi! How can I help?"
    assert run.stop_reason is StopReason.FINAL_ANSWER
    assert run.llm_calls == 1
    assert run.tool_steps == []
    assert [type(m) for m in run.history] == [HumanMessage, AIMessage]


def test_tool_call_then_answer(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(
        responses=[
            ai("", call("get_my_assets", call_id="a1"), tokens=(100, 5)),
            ai("You have a Dell Latitude 7440 (NS-LT-0101).", tokens=(180, 12)),
        ]
    )

    run = make_agent(model, repository, prompt).run("What laptop do I have?", user=aisha)

    assert run.answer == "You have a Dell Latitude 7440 (NS-LT-0101)."
    assert [type(s) for s in run.trajectory] == [ModelStep, ToolStep, ModelStep]
    tool_step = run.tool_steps[0]
    assert (tool_step.tool_name, tool_step.status) == ("get_my_assets", OutcomeStatus.OK)
    assert run.usage == TokenUsage(input_tokens=280, output_tokens=17)

    # The second model call saw the tool result, linked by the tool-call ID.
    second_call = model.calls[1]
    tool_message = second_call[-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "a1"
    assert "NS-LT-0101" in tool_message.text


def test_system_prompt_is_sent_first_and_not_stored_in_history(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("ok")])

    run = make_agent(model, repository, prompt).run("hello", user=aisha)

    sent = model.calls[0]
    assert isinstance(sent[0], SystemMessage) and sent[0].text == prompt.system
    assert not any(isinstance(m, SystemMessage) for m in run.history)


def test_tool_errors_are_returned_to_the_model(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(
        responses=[
            ai("", call("get_ticket", {"ticket_id": "INC-1003"})),
            ai("I couldn't find ticket INC-1003 for you."),
        ]
    )

    run = make_agent(model, repository, prompt).run("Show INC-1003", user=aisha)

    assert run.tool_steps[0].error_category == "not_found"
    tool_message = model.calls[1][-1]
    assert isinstance(tool_message, ToolMessage) and tool_message.status == "error"
    assert run.stop_reason is StopReason.FINAL_ANSWER


def test_history_carries_over_between_turns(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("", call("get_my_assets")), ai("A Dell."), ai("Yes.")])
    agent = make_agent(model, repository, prompt)

    first = agent.run("What laptop do I have?", user=aisha)
    agent.run("Is it a Dell?", user=aisha, history=first.history)

    third_call = model.calls[2]
    assert [m.type for m in third_call] == ["system", "human", "ai", "tool", "ai", "human"]


def test_step_limit_stops_a_looping_model(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    looping = [ai("", call("list_my_tickets", call_id=f"c{i}")) for i in range(10)]
    model = ScriptedChatModel(responses=looping)
    agent = make_agent(model, repository, prompt, AgentLimits(max_steps=3, max_tool_calls=50))

    run = agent.run("tickets?", user=aisha)

    assert run.stop_reason is StopReason.MAX_STEPS
    assert run.answer == STEP_LIMIT_ANSWER
    assert run.llm_calls == 3
    assert len(model.calls) == 3
    assert isinstance(run.history[-1], AIMessage)


def test_tool_call_limit_answers_every_requested_call(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(
        responses=[ai("", *(call("get_my_assets", call_id=f"c{i}") for i in range(3)))]
    )
    agent = make_agent(model, repository, prompt, AgentLimits(max_steps=5, max_tool_calls=2))

    run = agent.run("assets x3", user=aisha)

    assert run.stop_reason is StopReason.MAX_TOOL_CALLS
    assert run.answer == TOOL_LIMIT_ANSWER
    assert len(run.tool_steps) == 2
    tool_messages = [m for m in run.history if isinstance(m, ToolMessage)]
    # Providers reject a tool call without a result, so the skipped one is answered too.
    assert [m.tool_call_id for m in tool_messages] == ["c0", "c1", "c2"]
    assert json.loads(tool_messages[-1].text)["error"]["category"] == "not_executed"


def test_request_id_is_recorded(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("ok")])
    run = make_agent(model, repository, prompt).run("hi", user=aisha, request_id="req-42")
    assert run.request_id == "req-42"
    assert (run.prompt_name, run.prompt_version) == ("service_desk", "v1")


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError):
        AgentLimits(max_steps=0)
