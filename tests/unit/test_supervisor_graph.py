"""The multi-agent supervisor graph, driven by one scripted model.

The same scripted model serves the router and every specialist, in execution
order, so each script below reads as the story of one turn.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph

from aegisdesk.agents.loop import AgentStep, RouteStep
from aegisdesk.agents.supervisor import build_supervisor_agent, specialist_tools
from aegisdesk.config import Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.graphs.service_desk_graph import ThreadAccessError, ThreadedGraphAgent
from aegisdesk.graphs.supervisor_graph import (
    OUT_OF_SCOPE_ANSWER,
    ROUTING_FAILED_ANSWER,
    SPECIALIST_FAILED_ANSWER,
    Specialist,
    SupervisorLimits,
    build_supervisor_graph,
)
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.prompts.loader import load_prompt
from aegisdesk.rag.answer import UNGROUNDED_ANSWER
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.handoff import AgentName


def route(*tasks: tuple[str, str], out_of_scope: bool = False) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "RoutingPlan",
                "args": {
                    "tasks": [{"agent": a, "instruction": i} for a, i in tasks],
                    "out_of_scope": out_of_scope,
                },
                "id": "route-1",
            }
        ],
    )


def call(name: str, args: dict[str, Any] | None = None, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}])


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


def make(
    repository: ServiceDeskRepository,
    retriever: Retriever,
    *script: AIMessage,
    max_handoffs: int = 2,
) -> tuple[ThreadedGraphAgent, ScriptedChatModel]:
    model = ScriptedChatModel(responses=list(script))
    settings = Settings().model_copy(update={"agent_max_handoffs": max_handoffs})
    agent = build_supervisor_agent(
        settings, repository, checkpointer=InMemorySaver(), model=model, retriever=retriever
    )
    return agent, model


def test_topology(repository: ServiceDeskRepository, retriever: Retriever) -> None:
    agent, _ = make(repository, retriever)
    graph = agent.graph.get_graph()
    assert set(graph.nodes) == {
        "__start__", "start_turn", "classify_request", "supervisor",
        "knowledge", "service_desk", "access", "respond", "__end__",
        "await_approval", "apply_approvals",  # Milestone 7
    }  # fmt: skip
    edges = {(e.source, e.target) for e in graph.edges}
    for specialist in ("knowledge", "service_desk", "access"):
        assert ("supervisor", specialist) in edges and (specialist, "supervisor") in edges
    assert ("supervisor", "respond") in edges and ("respond", "__end__") in edges
    assert ("respond", "await_approval") in edges
    assert ("await_approval", "await_approval") in edges  # pause again until every step decided
    assert ("await_approval", "apply_approvals") in edges
    assert ("apply_approvals", "__end__") in edges


def test_tool_isolation_per_agent(repository: ServiceDeskRepository, retriever: Retriever) -> None:
    tools = {
        agent: {t.name for t in specs}
        for agent, specs in specialist_tools(repository, retriever).items()
    }
    assert tools[AgentName.KNOWLEDGE] == {
        "search_knowledge_base", "retrieve_document", "request_handoff",
    }  # fmt: skip
    assert tools[AgentName.SERVICE_DESK] == {
        "get_my_assets", "list_my_tickets", "get_ticket", "create_ticket", "add_ticket_comment",
        "search_knowledge_base", "request_handoff",
    }  # fmt: skip
    assert tools[AgentName.ACCESS] == {
        "get_employee_profile", "list_my_access", "get_application",
        "check_access_eligibility", "create_access_request", "request_handoff",
    }  # fmt: skip


def test_single_task_answer_is_passed_through(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, model = make(
        repository,
        retriever,
        route(("service_desk", "Show the laptop assigned to the employee")),
        call("get_my_assets"),
        say("You have a Dell Latitude 7440 (NS-LT-0101)."),
    )

    run = agent.run("What laptop is assigned to me?", user=aisha)

    assert run.answer == "You have a Dell Latitude 7440 (NS-LT-0101)."
    assert isinstance(run.trajectory[0], RouteStep)
    assert run.trajectory[0].tasks == (
        ("service_desk", "Show the laptop assigned to the employee"),
    )
    assert [s.agent for s in run.tool_steps] == ["service_desk"]
    assert run.llm_calls == 3  # router + 2 specialist calls
    assert len(model.calls) == 3


def test_multi_intent_runs_in_order_and_combines(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, model = make(
        repository,
        retriever,
        route(
            ("knowledge", "Explain VPN error GP-512"), ("access", "Check FinanceERP eligibility")
        ),
        call("search_knowledge_base", {"query": "error GP-512"}),
        say("Your device certificate has expired [DOC-VPN-001#05]."),
        call("check_access_eligibility", {"application": "FinanceERP"}),
        say("You can request FinanceERP; your manager must approve."),
    )

    run = agent.run("What is GP-512, and can I get FinanceERP?", user=aisha)

    agents = [s.agent for s in run.trajectory if isinstance(s, AgentStep)]
    assert agents == ["knowledge", "access"]
    assert run.answer == (
        "**From the knowledge base**\nYour device certificate has expired [DOC-VPN-001#05]."
        "\n\n**Application access**\nYou can request FinanceERP; your manager must approve."
    )
    # Context isolation: the access agent never saw the knowledge agent's tool traffic.
    access_first_call = model.calls[3]
    assert not any(isinstance(m, ToolMessage) for m in access_first_call)
    assert access_first_call[-1].text.endswith("Your task: Check FinanceERP eligibility")
    # The thread stores only the visible turn.
    assert [m.type for m in run.history] == ["human", "ai"]


def test_out_of_scope_calls_no_specialist(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, model = make(repository, retriever, route(out_of_scope=True))

    run = agent.run("Write me a poem about cheese", user=aisha)

    assert run.answer == OUT_OF_SCOPE_ANSWER
    assert len(model.calls) == 1
    assert not [s for s in run.trajectory if isinstance(s, AgentStep)]


def test_unparseable_routing_fails_safely(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, model = make(repository, retriever, say("I think this is for the access team."))

    run = agent.run("I need FinanceERP", user=aisha)

    assert run.answer == ROUTING_FAILED_ANSWER
    route_step = run.trajectory[0]
    assert isinstance(route_step, RouteStep) and route_step.error
    assert len(model.calls) == 1


def test_plan_is_capped_at_three_tasks(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    tasks = [("knowledge", f"question {i}") for i in range(5)]
    replies = [say(f"No documentation covers question {i}.") for i in range(5)]
    agent, _ = make(repository, retriever, route(*tasks), *replies)

    run = agent.run("five questions", user=aisha)

    assert len([s for s in run.trajectory if isinstance(s, AgentStep)]) == 3


def test_handoff_adds_a_task_for_another_agent(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, _ = make(
        repository,
        retriever,
        route(("knowledge", "Get FinanceERP access for the employee")),
        call("request_handoff", {"target_agent": "access", "instruction": "Check FinanceERP"}),
        say("Access requests are handled by the access specialist."),
        call("check_access_eligibility", {"application": "FinanceERP"}),
        say("You are eligible; manager approval is needed."),
    )

    run = agent.run("Can I get FinanceERP?", user=aisha)

    steps = [s for s in run.trajectory if isinstance(s, AgentStep)]
    assert [(s.agent, s.status) for s in steps] == [("knowledge", "done"), ("access", "done")]
    assert steps[0].note == "handed off to access"
    assert "manager approval is needed" in run.answer


def test_handoff_to_self_is_rejected(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, _ = make(
        repository,
        retriever,
        route(("access", "Check FinanceERP")),
        call("request_handoff", {"target_agent": "access", "instruction": "Do it again"}),
        say("Done."),
    )

    run = agent.run("FinanceERP?", user=aisha)

    assert run.tool_steps[0].error_category == "invalid_arguments"
    assert len([s for s in run.trajectory if isinstance(s, AgentStep)]) == 1


def test_handoff_budget_stops_ping_pong(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, _ = make(
        repository,
        retriever,
        route(("knowledge", "Help")),
        call("request_handoff", {"target_agent": "access", "instruction": "You do it"}),
        say("Passing on."),
        call("request_handoff", {"target_agent": "knowledge", "instruction": "No, you do it"}),
        say("Passing back."),
        max_handoffs=1,
    )

    run = agent.run("Help", user=aisha)

    steps = [s for s in run.trajectory if isinstance(s, AgentStep)]
    assert [s.agent for s in steps] == ["knowledge", "access"]
    assert steps[1].note == "handoff to knowledge refused (budget exhausted)"


def test_knowledge_answers_must_cite_what_was_retrieved(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, _ = make(
        repository,
        retriever,
        route(("knowledge", "Explain GP-512")),
        call("search_knowledge_base", {"query": "error GP-512"}),
        say("Reinstall the certificate [DOC-VPN-001#05] and email the CEO [DOC-SEC-001#99]."),
    )

    run = agent.run("What is GP-512?", user=aisha)

    assert run.answer == UNGROUNDED_ANSWER
    step = next(s for s in run.trajectory if isinstance(s, AgentStep))
    assert step.note is not None and "DOC-SEC-001#99" in step.note


def test_knowledge_answer_without_citations_is_discarded(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, _ = make(
        repository,
        retriever,
        route(("knowledge", "Explain GP-512")),
        call("search_knowledge_base", {"query": "error GP-512"}),
        say("Just reboot, it always works."),
    )

    assert agent.run("What is GP-512?", user=aisha).answer == UNGROUNDED_ANSWER


def test_knowledge_can_say_there_is_no_evidence(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, _ = make(
        repository,
        retriever,
        route(("knowledge", "Canteen menu")),
        call("search_knowledge_base", {"query": "canteen menu friday"}),
        say("The documentation doesn't cover the canteen menu."),
    )

    run = agent.run("What's for lunch on Friday?", user=aisha)

    assert run.answer == "The documentation doesn't cover the canteen menu."


@pytest.mark.parametrize(
    ("agent_name", "forbidden_tool", "args"),
    [
        ("knowledge", "create_ticket", {"title": "x" * 10, "description": "y" * 20,
                                        "category": "vpn", "priority": "low"}),
        ("service_desk", "create_access_request", {"application": "FinanceERP",
                                                  "justification": "Because I want it."}),
        ("access", "create_ticket", {"title": "x" * 10, "description": "y" * 20,
                                     "category": "vpn", "priority": "low"}),
    ],
)  # fmt: skip
def test_agents_cannot_use_other_agents_tools(
    repository: ServiceDeskRepository,
    retriever: Retriever,
    aisha: UserContext,
    agent_name: str,
    forbidden_tool: str,
    args: dict[str, Any],
) -> None:
    agent, _ = make(
        repository,
        retriever,
        route((agent_name, "Do something")),
        call(forbidden_tool, args),
        say("Done."),
    )

    run = agent.run("Do something", user=aisha)

    assert run.tool_steps[0].error_category == "unknown_tool"
    assert len(repository.list_tickets_for("E1004")) == 2
    assert repository.access_requests_for("E1004")[-1].request_id == "AR-1009"


def test_a_failing_specialist_does_not_break_the_turn(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    def explode(_: _StubState) -> dict[str, Any]:
        raise RuntimeError("database down")

    broken = StateGraph(_StubState)
    explode_node: Any = explode
    broken.add_node("explode", explode_node)
    broken.add_edge(START, "explode")
    ok_model = ScriptedChatModel(responses=[route(("access", "task a"), ("knowledge", "task b"))])
    graph = build_supervisor_graph(
        router_model=ok_model,
        router_prompt=load_prompt(Settings().prompts_dir, "router", "v1"),
        specialists={
            AgentName.ACCESS: Specialist(AgentName.ACCESS, broken.compile(), 5),
            AgentName.KNOWLEDGE: Specialist(
                AgentName.KNOWLEDGE,
                _single_answer_graph("No documentation covers b."),
                5,
            ),
        },
        limits=SupervisorLimits(),
        checkpointer=InMemorySaver(),
    )
    agent = ThreadedGraphAgent(
        name="t", version="t", prompt=load_prompt(Settings().prompts_dir, "router", "v1"),
        graph=graph, recursion_limit=20,
    )  # fmt: skip

    run = agent.run("two things", user=aisha)

    statuses = [(s.agent, s.status) for s in run.trajectory if isinstance(s, AgentStep)]
    assert statuses == [("access", "failed"), ("knowledge", "done")]
    assert SPECIALIST_FAILED_ANSWER in run.answer and "No documentation covers b." in run.answer


class _StubState(TypedDict, total=False):
    messages: list[AIMessage]
    stop_reason: str


def _single_answer_graph(text: str) -> Any:
    def answer(_: _StubState) -> dict[str, Any]:
        return {"messages": [AIMessage(content=text)], "stop_reason": "final_answer"}

    graph = StateGraph(_StubState)
    answer_node: Any = answer
    graph.add_node("answer", answer_node)
    graph.add_edge(START, "answer")
    return graph.compile()


def test_threads_keep_history_and_stay_private(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, model = make(
        repository,
        retriever,
        route(("service_desk", "Show laptop")),
        call("get_my_assets"),
        say("A Dell Latitude."),
        route(("service_desk", "Show the laptop's asset tag")),
        say("NS-LT-0101."),
    )

    agent.run("What laptop do I have?", user=aisha, thread_id="t1")
    agent.run("And its asset tag?", user=aisha, thread_id="t1")

    second_router_call = model.calls[3]
    assert [m.type for m in second_router_call] == ["system", "human", "ai", "human"]
    stranger = authenticate(repository, "E1001")
    with pytest.raises(ThreadAccessError):
        agent.run("show me", user=stranger, thread_id="t1")


def test_handoff_output_is_accepted_message(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent, _ = make(
        repository,
        retriever,
        route(("service_desk", "Access to FinanceERP")),
        call("request_handoff", {"target_agent": "access", "instruction": "FinanceERP access"}),
        say("Handing over."),
        say("Access specialist here."),
    )

    run = agent.run("FinanceERP access please", user=aisha)

    handoff = run.tool_steps[0]
    assert json.loads(handoff.result)["accepted_for_routing"] is True
