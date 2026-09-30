"""The Service Desk LangGraph: topology, behaviour, limits and streaming."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langgraph.checkpoint.memory import InMemorySaver

from aegisdesk.agents.loop import (
    STEP_LIMIT_ANSWER,
    TOOL_LIMIT_ANSWER,
    AgentLimits,
    ModelStep,
    StopReason,
    ToolStep,
)
from aegisdesk.config import PROJECT_ROOT
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.graphs.service_desk_graph import (
    NODE_LIMIT,
    NODE_MODEL,
    NODE_START,
    NODE_TOOLS,
    ServiceDeskGraphAgent,
)
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
) -> ServiceDeskGraphAgent:
    return ServiceDeskGraphAgent(
        name="service_desk",
        version="test",
        model=model,
        prompt=prompt,
        executor=ToolExecutor(build_service_desk_tools(repository)),
        limits=limits or AgentLimits(),
        checkpointer=InMemorySaver(),
    )


def test_topology(repository: ServiceDeskRepository, prompt: Prompt) -> None:
    graph = make_agent(ScriptedChatModel(), repository, prompt).graph.get_graph()

    assert set(graph.nodes) == {
        "__start__",
        NODE_START,
        NODE_MODEL,
        NODE_TOOLS,
        NODE_LIMIT,
        "__end__",
    }
    edges = {(e.source, e.target, e.conditional) for e in graph.edges}
    assert edges == {
        ("__start__", NODE_START, False),
        (NODE_START, NODE_MODEL, False),
        (NODE_MODEL, NODE_TOOLS, True),
        (NODE_MODEL, "__end__", True),
        (NODE_TOOLS, NODE_MODEL, True),
        (NODE_TOOLS, NODE_LIMIT, True),
        (NODE_LIMIT, "__end__", False),
    }


def test_answer_without_tools(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("Hi! How can I help?")])

    run = make_agent(model, repository, prompt).run("hello", user=aisha, thread_id="t")

    assert run.answer == "Hi! How can I help?"
    assert run.stop_reason is StopReason.FINAL_ANSWER
    assert run.llm_calls == 1
    assert run.thread_id == "t"


def test_tool_call_then_answer(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(
        responses=[
            ai("", call("get_my_assets", call_id="a1"), tokens=(100, 5)),
            ai("You have a Dell Latitude 7440.", tokens=(180, 12)),
        ]
    )

    run = make_agent(model, repository, prompt).run("What laptop?", user=aisha)

    assert run.answer == "You have a Dell Latitude 7440."
    assert [type(s) for s in run.trajectory] == [ModelStep, ToolStep, ModelStep]
    assert run.tool_steps[0].status is OutcomeStatus.OK
    assert run.usage == TokenUsage(input_tokens=280, output_tokens=17)
    tool_message = model.calls[1][-1]
    assert isinstance(tool_message, ToolMessage) and tool_message.tool_call_id == "a1"
    assert "NS-LT-0101" in tool_message.text


def test_system_prompt_is_sent_but_not_stored_in_the_thread(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("ok")])
    agent = make_agent(model, repository, prompt)

    agent.run("hello", user=aisha, thread_id="t")

    assert isinstance(model.calls[0][0], SystemMessage)
    assert not any(isinstance(m, SystemMessage) for m in agent.history("t", aisha))


def test_turns_in_a_thread_share_history(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("", call("get_my_assets")), ai("A Dell."), ai("Yes.")])
    agent = make_agent(model, repository, prompt)

    agent.run("What laptop do I have?", user=aisha, thread_id="t")
    second = agent.run("Is it a Dell?", user=aisha, thread_id="t")

    assert [m.type for m in model.calls[2]] == ["system", "human", "ai", "tool", "ai", "human"]
    # Per-turn bookkeeping is reset: the second turn made one model call and no tool calls.
    assert second.llm_calls == 1
    assert second.tool_steps == []


def test_separate_threads_are_isolated(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("first"), ai("second")])
    agent = make_agent(model, repository, prompt)

    agent.run("one", user=aisha, thread_id="a")
    agent.run("two", user=aisha, thread_id="b")

    assert [m.type for m in model.calls[1]] == ["system", "human"]


def test_step_limit(repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt) -> None:
    looping = [ai("", call("list_my_tickets", call_id=f"c{i}")) for i in range(10)]
    agent = make_agent(
        ScriptedChatModel(responses=looping),
        repository,
        prompt,
        AgentLimits(max_steps=3, max_tool_calls=50),
    )

    run = agent.run("tickets?", user=aisha)

    assert run.stop_reason is StopReason.MAX_STEPS
    assert run.answer == STEP_LIMIT_ANSWER
    assert run.llm_calls == 3


def test_tool_call_limit_answers_every_call(
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
    assert [m.tool_call_id for m in tool_messages] == ["c0", "c1", "c2"]
    assert json.loads(tool_messages[-1].text)["error"]["category"] == "not_executed"


def test_streaming_reports_each_node_in_order(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    model = ScriptedChatModel(responses=[ai("", call("get_my_assets")), ai("done")])
    seen: list[str] = []

    make_agent(model, repository, prompt).run(
        "laptop?", user=aisha, on_update=lambda node, _update: seen.append(node)
    )

    assert seen == [NODE_START, NODE_MODEL, NODE_TOOLS, NODE_MODEL]


def test_checkpoint_is_saved_after_every_node(
    repository: ServiceDeskRepository, aisha: UserContext, prompt: Prompt
) -> None:
    agent = make_agent(
        ScriptedChatModel(responses=[ai("", call("get_my_assets")), ai("done")]),
        repository,
        prompt,
    )

    agent.run("laptop?", user=aisha, thread_id="t")

    history = list(agent.graph.get_state_history({"configurable": {"thread_id": "t"}}))
    # Newest first: each snapshot records which node(s) run next.
    next_nodes = [snapshot.next for snapshot in history]
    assert next_nodes[:5] == [(), (NODE_MODEL,), (NODE_TOOLS,), (NODE_MODEL,), (NODE_START,)]
