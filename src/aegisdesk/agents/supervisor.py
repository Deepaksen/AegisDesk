"""Assemble the multi-agent system: a supervisor and three specialist subgraphs.

Tool isolation is decided here, in one place:

    knowledge    : search_knowledge_base, retrieve_document, request_handoff
    service_desk : get_my_assets, list_my_tickets, get_ticket, create_ticket,
                   search_knowledge_base (read-only, for troubleshooting), request_handoff
    access       : get_employee_profile, list_my_access, get_application,
                   check_access_eligibility, create_access_request, request_handoff
    supervisor   : none
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from aegisdesk.agents.knowledge import check_knowledge_answer
from aegisdesk.agents.loop import AgentLimits
from aegisdesk.config import Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.graphs.service_desk_graph import ThreadedGraphAgent, build_tool_agent_graph
from aegisdesk.graphs.supervisor_graph import (
    Specialist,
    SupervisorLimits,
    build_supervisor_graph,
    supervisor_recursion_limit,
)
from aegisdesk.llm.factory import build_chat_model
from aegisdesk.prompts.loader import load_prompt
from aegisdesk.rag.factory import build_retriever
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.tools.access import build_access_tools
from aegisdesk.tools.base import ToolSpec
from aegisdesk.tools.executor import ToolExecutor
from aegisdesk.tools.handoff import AgentName, build_handoff_tool
from aegisdesk.tools.knowledge import build_knowledge_tools
from aegisdesk.tools.service_desk import build_service_desk_tools

SUPERVISOR_NAME = "supervisor"
SUPERVISOR_VERSION = "0.1.0"

PROMPTS = {
    AgentName.KNOWLEDGE: ("knowledge", "v1"),
    AgentName.SERVICE_DESK: ("service_desk", "v3"),
    AgentName.ACCESS: ("access", "v1"),
}


def specialist_tools(
    repository: ServiceDeskRepository, retriever: Retriever
) -> dict[AgentName, list[ToolSpec[Any, Any]]]:
    knowledge = build_knowledge_tools(retriever)
    search_only = [t for t in knowledge if t.name == "search_knowledge_base"]
    return {
        AgentName.KNOWLEDGE: [*knowledge, build_handoff_tool(AgentName.KNOWLEDGE)],
        AgentName.SERVICE_DESK: [
            *build_service_desk_tools(repository),
            *search_only,
            build_handoff_tool(AgentName.SERVICE_DESK),
        ],
        AgentName.ACCESS: [*build_access_tools(repository), build_handoff_tool(AgentName.ACCESS)],
    }


def build_supervisor_agent(
    settings: Settings,
    repository: ServiceDeskRepository,
    *,
    checkpointer: BaseCheckpointSaver[Any],
    model: BaseChatModel | None = None,
    retriever: Retriever | None = None,
) -> ThreadedGraphAgent:
    model = model if model is not None else build_chat_model(settings)
    retriever = retriever or build_retriever(settings)
    agent_limits = AgentLimits(
        max_steps=settings.agent_max_steps, max_tool_calls=settings.agent_max_tool_calls
    )
    tools = specialist_tools(repository, retriever)

    specialists = {}
    for agent, (prompt_name, version) in PROMPTS.items():
        specialists[agent] = Specialist(
            agent=agent,
            graph=build_tool_agent_graph(
                model=model,
                prompt=load_prompt(settings.prompts_dir, prompt_name, version),
                executor=ToolExecutor(tools[agent]),
                limits=agent_limits,
                checkpointer=False,  # the parent thread stores only what specialists return
            ),
            recursion_limit=2 * agent_limits.max_steps + 4,
            check_answer=check_knowledge_answer if agent is AgentName.KNOWLEDGE else None,
        )

    limits = SupervisorLimits(max_handoffs=settings.agent_max_handoffs)
    router_prompt = load_prompt(settings.prompts_dir, "router", "v1")
    return ThreadedGraphAgent(
        name=SUPERVISOR_NAME,
        version=SUPERVISOR_VERSION,
        prompt=router_prompt,
        graph=build_supervisor_graph(
            router_model=model,
            router_prompt=router_prompt,
            specialists=specialists,
            limits=limits,
            checkpointer=checkpointer,
        ),
        recursion_limit=supervisor_recursion_limit(limits),
    )
