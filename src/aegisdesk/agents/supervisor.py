"""Assemble the multi-agent system: a supervisor and three specialist subgraphs.

Tool isolation is decided here, in one place:

    knowledge    : search_knowledge_base, retrieve_document, request_handoff
    service_desk : get_my_assets, list_my_tickets, get_ticket, create_ticket,
                   add_ticket_comment, search_knowledge_base (read-only, for
                   troubleshooting), request_handoff
    access       : get_employee_profile, list_my_access, get_application,
                   check_access_eligibility, create_access_request, request_handoff
    supervisor   : none

From Milestone 5 the enterprise tools can run behind MCP servers
(`TOOL_TRANSPORT`); the lists above stay the allowlist either way.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from aegisdesk.agents.knowledge import check_knowledge_answer
from aegisdesk.agents.loop import AgentLimits
from aegisdesk.approvals.service import ApprovalService
from aegisdesk.approvals.workflow import AccessApprovalWorkflow, workflow_identity
from aegisdesk.config import Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.factory import build_gateway
from aegisdesk.graphs.service_desk_graph import ThreadedGraphAgent, build_tool_agent_graph
from aegisdesk.graphs.supervisor_graph import (
    Specialist,
    SupervisorLimits,
    build_supervisor_graph,
    supervisor_recursion_limit,
)
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.llm.factory import build_chat_model
from aegisdesk.prompts.loader import load_prompt
from aegisdesk.rag.factory import build_retriever
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.reliability.model_guard import guard_for
from aegisdesk.tools.access import build_access_tools
from aegisdesk.tools.base import ToolSpec
from aegisdesk.tools.handoff import AgentName, build_handoff_tool
from aegisdesk.tools.knowledge import build_knowledge_tools
from aegisdesk.tools.provisioning import build_provisioning_tools
from aegisdesk.tools.service_desk import build_service_desk_tools
from aegisdesk.tools.transport import ToolFactory

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


def specialist_identity(agent: AgentName, settings: Settings) -> AgentIdentity:
    """The agent identity carried in every MCP delegation token this specialist uses."""
    return AgentIdentity(
        agent_id=agent.value,
        agent_version=SUPERVISOR_VERSION,
        agent_type="specialist",
        environment=settings.aegis_env.value,
    )


def build_supervisor_agent(
    settings: Settings,
    repository: ServiceDeskRepository,
    *,
    checkpointer: BaseCheckpointSaver[Any],
    model: BaseChatModel | None = None,
    retriever: Retriever | None = None,
    tool_factory: ToolFactory | None = None,
) -> ThreadedGraphAgent:
    """`tool_factory` decides where enterprise tools run (local by default).

    The caller owns the factory and closes it when done with the agent.
    """
    model = model if model is not None else build_chat_model(settings)
    guard = guard_for(model, settings)  # one breaker per model, shared by all its callers
    retriever = retriever or build_retriever(settings)
    agent_limits = AgentLimits(
        max_steps=settings.agent_max_steps, max_tool_calls=settings.agent_max_tool_calls
    )
    tools = specialist_tools(repository, retriever)
    tool_factory = tool_factory or ToolFactory(
        gateway=build_gateway(settings, access_store=repository.access_store)
    )

    specialists = {}
    for agent, (prompt_name, version) in PROMPTS.items():
        specialists[agent] = Specialist(
            agent=agent,
            graph=build_tool_agent_graph(
                model=model,
                prompt=load_prompt(settings.prompts_dir, prompt_name, version),
                executor=tool_factory.runner(specialist_identity(agent, settings), tools[agent]),
                limits=agent_limits,
                checkpointer=False,  # the parent thread stores only what specialists return
                guard=guard,
            ),
            recursion_limit=2 * agent_limits.max_steps + 4,
            check_answer=check_knowledge_answer if agent is AgentName.KNOWLEDGE else None,
        )

    # The approval workflow (M7): deterministic code with its own identity, which
    # the policy allows to provision only with recorded approval.
    gateway = tool_factory.gateway
    workflow = AccessApprovalWorkflow(
        repository,
        ApprovalService(repository.access_store, gateway.audit, environment=gateway.environment),
        tool_factory.runner(
            workflow_identity(settings.aegis_env.value), build_provisioning_tools(repository)
        ),
    )

    limits = SupervisorLimits(max_handoffs=settings.agent_max_handoffs)
    router_prompt = load_prompt(settings.prompts_dir, "router", "v1")
    return ThreadedGraphAgent(
        name=SUPERVISOR_NAME,
        version=SUPERVISOR_VERSION,
        prompt=router_prompt,
        graph=build_supervisor_graph(
            router_model=model,
            router_guard=guard,
            router_prompt=router_prompt,
            specialists=specialists,
            limits=limits,
            checkpointer=checkpointer,
            approvals=workflow,
        ),
        recursion_limit=supervisor_recursion_limit(limits),
    )
