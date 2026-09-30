"""The multi-agent graph: a supervisor routing work to specialist subgraphs.

    START → start_turn → classify_request → supervisor ─┬─► knowledge ──────┐
                           (LLM: RoutingPlan)    ▲  │    ├─► service_desk ──┤  specialist
                                                 │  │    ├─► access ────────┤  subgraphs
                                                 └──┼────┴──────────────────┘
                                                    └─► respond → END

Who decides what:

* The **model** decides *what the user wants*: `classify_request` turns the
  latest message into a `RoutingPlan` (tasks for specialists). Specialists
  decide which of *their own* tools to call.
* **Code** decides *what happens*: the `supervisor` node is deterministic. It
  runs pending tasks in order, returns `Command(goto=...)` to the right
  specialist, and ends the turn. It has no tools and cannot be talked into
  calling one. Handoffs requested by specialists are accepted only within a
  budget, never to the same agent, never as a duplicate task.

Isolation:

* **Tool isolation** - each specialist subgraph has its own `ToolExecutor`
  with only its own tools, so the Knowledge agent *cannot* create a ticket.
* **Context isolation** - a specialist receives the visible conversation plus
  its own task, runs in its own state, and returns only its answer and
  trajectory. Its private tool messages never enter the parent thread or
  another specialist's context.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import ValidationError

from aegisdesk.agents.loop import StopReason
from aegisdesk.graphs.state import ServiceDeskState
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.prompts.loader import Prompt
from aegisdesk.schemas.routing import RoutingPlan
from aegisdesk.tools.handoff import HANDOFF_TOOL, AgentName

logger = logging.getLogger(__name__)

NODE_START = "start_turn"
NODE_CLASSIFY = "classify_request"
NODE_SUPERVISOR = "supervisor"
NODE_RESPOND = "respond"
SPECIALIST_NODES = {agent: agent.value for agent in AgentName}

# Visible turns (human/assistant messages) passed to the router and to specialists.
HISTORY_WINDOW = 6

OUT_OF_SCOPE_ANSWER = (
    "I can help with IT questions, company IT policies, your equipment and tickets, and "
    "application access. That request is outside what I can help with."
)
ROUTING_FAILED_ANSWER = (
    "Sorry, I couldn't work out how to handle that request. Could you rephrase it, or split it "
    "into separate questions?"
)
SPECIALIST_FAILED_ANSWER = (
    "This part of your request could not be completed. Please try again later."
)

SECTION_TITLES = {
    AgentName.KNOWLEDGE: "From the knowledge base",
    AgentName.SERVICE_DESK: "Equipment and tickets",
    AgentName.ACCESS: "Application access",
}


class SupervisorState(ServiceDeskState, total=False):
    # Tasks for this turn: {agent, instruction, status, answer, source}
    plan: list[dict[str, Any]]
    handoffs: int
    out_of_scope: bool
    routing_error: str | None


# (answer, specialist trajectory entries) -> (answer to use, note or None)
AnswerCheck = Callable[[str, list[dict[str, Any]]], tuple[str, str | None]]


@dataclass(frozen=True)
class Specialist:
    agent: AgentName
    graph: CompiledStateGraph[Any, Any, Any, Any]
    recursion_limit: int
    check_answer: AnswerCheck | None = None


@dataclass(frozen=True)
class SupervisorLimits:
    max_tasks: int = 3  # from the routing plan
    max_handoffs: int = 2  # extra tasks specialists may add

    @property
    def max_specialist_runs(self) -> int:
        return self.max_tasks + self.max_handoffs


def _visible(messages: list[BaseMessage]) -> list[BaseMessage]:
    visible: list[BaseMessage] = [m for m in messages if isinstance(m, HumanMessage | AIMessage)]
    return visible[-HISTORY_WINDOW:]


def _pending(plan: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((t for t in plan if t["status"] == "pending"), None)


def build_supervisor_graph(
    *,
    router_model: BaseChatModel,
    router_prompt: Prompt,
    specialists: dict[AgentName, Specialist],
    limits: SupervisorLimits,
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    router = router_model.with_structured_output(RoutingPlan, include_raw=True)

    # -- nodes -------------------------------------------------------------------

    def start_turn(state: SupervisorState) -> dict[str, Any]:
        return {
            "owner_id": state.get("owner_id") or state["user"]["employee_id"],
            "llm_calls": 0,
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "stop_reason": None,
            "trajectory": [],
            "plan": [],
            "handoffs": 0,
            "out_of_scope": False,
            "routing_error": None,
        }

    def classify_request(state: SupervisorState) -> dict[str, Any]:
        started = time.perf_counter()
        messages = [SystemMessage(content=router_prompt.system), *_visible(state["messages"])]
        error: str | None = None
        plan: list[dict[str, Any]] = []
        out_of_scope = False
        usage = TokenUsage()
        try:
            result = cast(dict[str, Any], router.invoke(messages))
            raw = result.get("raw")
            if isinstance(raw, AIMessage):
                usage = TokenUsage.from_message(raw)
            parsed = result.get("parsed")
            if result.get("parsing_error") is not None or not isinstance(parsed, RoutingPlan):
                error = f"invalid routing output: {result.get('parsing_error')}"
            else:
                out_of_scope = parsed.out_of_scope and not parsed.tasks
                seen: set[tuple[str, str]] = set()
                for task in parsed.tasks[: limits.max_tasks]:
                    key = (task.agent.value, task.instruction.strip())
                    if key not in seen:
                        seen.add(key)
                        plan.append(
                            {
                                "agent": task.agent.value,
                                "instruction": task.instruction.strip(),
                                "status": "pending",
                                "answer": "",
                                "source": "router",
                            }
                        )
                if not plan and not out_of_scope:
                    error = "routing produced no tasks"
        except (ValidationError, ValueError) as exc:
            error = f"routing failed: {exc}"

        entry = {
            "kind": "route",
            "tasks": [{"agent": t["agent"], "instruction": t["instruction"]} for t in plan],
            "out_of_scope": out_of_scope,
            "error": error,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        return {
            "plan": plan,
            "out_of_scope": out_of_scope,
            "routing_error": error,
            "llm_calls": state["llm_calls"] + 1,
            "input_tokens": state["input_tokens"] + usage.input_tokens,
            "output_tokens": state["output_tokens"] + usage.output_tokens,
            "trajectory": [*state["trajectory"], entry],
        }

    def supervisor(state: SupervisorState) -> Command[str]:
        # Deterministic: no model call, no tools. Only decides where to go next.
        task = _pending(state.get("plan", []))
        if state.get("routing_error") or task is None:
            return Command(goto=NODE_RESPOND)
        if AgentName(task["agent"]) not in specialists:  # routed to an agent not deployed
            task["status"] = "failed"
            task["answer"] = SPECIALIST_FAILED_ANSWER
            return Command(goto=NODE_SUPERVISOR, update={"plan": state["plan"]})
        return Command(goto=SPECIALIST_NODES[AgentName(task["agent"])])

    def make_specialist_node(specialist: Specialist) -> Callable[[SupervisorState], dict[str, Any]]:
        name = specialist.agent

        def run_specialist(state: SupervisorState) -> dict[str, Any]:
            plan = [dict(t) for t in state["plan"]]
            task = next(t for t in plan if t["status"] == "pending" and t["agent"] == name.value)
            visible = _visible(state["messages"])
            request = visible[-1].text if visible else ""
            sub_input = {
                # Context isolation: visible conversation + this task only.
                "messages": [
                    *visible[:-1],
                    HumanMessage(content=f"{request}\n\nYour task: {task['instruction']}"),
                ],
                "user": state["user"],
                "request_id": state["request_id"],
            }
            note: str | None = None
            sub_trajectory: list[dict[str, Any]] = []
            sub: dict[str, Any] = {}
            try:
                sub = specialist.graph.invoke(
                    sub_input, {"recursion_limit": specialist.recursion_limit}
                )
                sub_trajectory = [dict(e, agent=name.value) for e in sub.get("trajectory", [])]
                answer = sub["messages"][-1].text
                status = (
                    "done"
                    if sub.get("stop_reason") == StopReason.FINAL_ANSWER.value
                    else "incomplete"
                )
                if specialist.check_answer is not None:
                    answer, note = specialist.check_answer(answer, sub_trajectory)
            except Exception:  # a failing specialist must not take the whole turn down
                logger.exception("Specialist %s failed (request_id=%s)", name, state["request_id"])
                answer, status, note = SPECIALIST_FAILED_ANSWER, "failed", "specialist error"

            task.update(status=status, answer=answer)
            handoffs = state["handoffs"]
            notes = [note] if note else []
            for entry in sub_trajectory:
                if entry.get("kind") != "tool" or entry["tool_name"] != HANDOFF_TOOL:
                    continue
                if entry["status"] != "ok":
                    continue
                target = entry["args"]["target_agent"]
                instruction = str(entry["args"]["instruction"]).strip()
                duplicate = any(
                    t["agent"] == target and t["instruction"] == instruction for t in plan
                )
                if duplicate:
                    notes.append(f"handoff to {target} ignored (duplicate)")
                elif handoffs >= limits.max_handoffs or len(plan) >= limits.max_specialist_runs:
                    notes.append(f"handoff to {target} refused (budget exhausted)")
                else:
                    handoffs += 1
                    plan.append(
                        {
                            "agent": target,
                            "instruction": instruction,
                            "status": "pending",
                            "answer": "",
                            "source": f"handoff:{name.value}",
                        }
                    )
                    notes.append(f"handed off to {target}")

            summary = {
                "kind": "agent",
                "agent": name.value,
                "instruction": task["instruction"],
                "status": status,
                "answer": answer,
                "note": "; ".join(notes) or None,
            }
            return {
                "plan": plan,
                "handoffs": handoffs,
                "llm_calls": state["llm_calls"] + sub.get("llm_calls", 0),
                "tool_calls": state["tool_calls"] + sub.get("tool_calls", 0),
                "input_tokens": state["input_tokens"] + sub.get("input_tokens", 0),
                "output_tokens": state["output_tokens"] + sub.get("output_tokens", 0),
                "trajectory": [*state["trajectory"], *sub_trajectory, summary],
            }

        return run_specialist

    def respond(state: SupervisorState) -> dict[str, Any]:
        finished = [t for t in state.get("plan", []) if t["status"] != "pending"]
        if state.get("routing_error"):
            answer = ROUTING_FAILED_ANSWER
        elif state.get("out_of_scope") or not finished:
            answer = OUT_OF_SCOPE_ANSWER
        elif len(finished) == 1:
            answer = finished[0]["answer"]  # no extra model call for a single specialist
        else:
            # Combined deterministically: sections, no new text that could add facts.
            answer = "\n\n".join(
                f"**{SECTION_TITLES[AgentName(t['agent'])]}**\n{t['answer']}" for t in finished
            )
        return {
            "messages": [AIMessage(content=answer)],
            "stop_reason": StopReason.FINAL_ANSWER.value,
        }

    graph = StateGraph(SupervisorState)
    graph.add_node(NODE_START, start_turn)
    graph.add_node(NODE_CLASSIFY, classify_request)
    graph.add_node(
        NODE_SUPERVISOR,
        supervisor,
        destinations=(*(SPECIALIST_NODES[a] for a in specialists), NODE_RESPOND),
    )
    for agent, specialist in specialists.items():
        node: Any = make_specialist_node(specialist)
        graph.add_node(SPECIALIST_NODES[agent], node)
        graph.add_edge(SPECIALIST_NODES[agent], NODE_SUPERVISOR)
    graph.add_node(NODE_RESPOND, respond)

    graph.add_edge(START, NODE_START)
    graph.add_edge(NODE_START, NODE_CLASSIFY)
    graph.add_edge(NODE_CLASSIFY, NODE_SUPERVISOR)
    graph.add_edge(NODE_RESPOND, END)
    return graph.compile(checkpointer=checkpointer)


def supervisor_recursion_limit(limits: SupervisorLimits) -> int:
    # start + classify + (supervisor + specialist) per run + final supervisor + respond
    return 2 * limits.max_specialist_runs + 6
