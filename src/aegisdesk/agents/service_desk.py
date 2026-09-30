"""The Service Desk agent: a tool-calling agent with exactly the Service Desk tools.

Two engines with identical behaviour:

* `build_service_desk_agent` - the hand-written loop from Milestone 1.
* `build_service_desk_graph_agent` - the LangGraph version from Milestone 2,
  with persisted threads.

From Milestone 3 the default prompt (v2) also gives the agent the knowledge
base tools. Prompt v1 reproduces the Milestone 1 agent exactly.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from aegisdesk.agents.loop import AgentLimits, ToolCallingAgent
from aegisdesk.config import Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.factory import build_gateway
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.graphs.service_desk_graph import ServiceDeskGraphAgent
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.llm.factory import build_chat_model
from aegisdesk.prompts.loader import load_prompt
from aegisdesk.rag.factory import build_retriever
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.reliability.model_guard import guard_for
from aegisdesk.tools.executor import ToolExecutor
from aegisdesk.tools.knowledge import build_knowledge_tools
from aegisdesk.tools.service_desk import build_service_desk_tools

AGENT_NAME = "service_desk"
# Policy identity of the single agent (config/policy.yaml: service_desk_single).
POLICY_AGENT_ID = "service_desk_single"
AGENT_VERSION = "0.2.0"
PROMPT_NAME = "service_desk"
DEFAULT_PROMPT_VERSION = "v2"
# Prompt v1 is the Milestone 1 agent (no knowledge base); v2 adds the knowledge tools.
_KNOWLEDGE_PROMPT_VERSIONS = frozenset({"v2"})


def _executor(
    settings: Settings,
    repository: ServiceDeskRepository,
    retriever: Retriever | None,
    prompt_version: str,
    gateway: ActionGateway | None = None,
) -> ToolExecutor:
    tools = build_service_desk_tools(repository)
    if prompt_version in _KNOWLEDGE_PROMPT_VERSIONS:
        tools += build_knowledge_tools(retriever or build_retriever(settings))
    identity = AgentIdentity(
        agent_id=POLICY_AGENT_ID,
        agent_version=AGENT_VERSION,
        agent_type="single_agent",
        environment=settings.aegis_env.value,
    )
    gateway = gateway or build_gateway(settings, access_store=repository.access_store)
    return ToolExecutor(tools, gateway=gateway, agent=identity)


def build_service_desk_agent(
    settings: Settings,
    repository: ServiceDeskRepository,
    *,
    model: BaseChatModel | None = None,
    retriever: Retriever | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> ToolCallingAgent:
    return ToolCallingAgent(
        name=AGENT_NAME,
        version=AGENT_VERSION,
        model=model if model is not None else build_chat_model(settings),
        prompt=load_prompt(settings.prompts_dir, PROMPT_NAME, prompt_version),
        executor=_executor(settings, repository, retriever, prompt_version),
        limits=AgentLimits(
            max_steps=settings.agent_max_steps, max_tool_calls=settings.agent_max_tool_calls
        ),
    )


def build_service_desk_graph_agent(
    settings: Settings,
    repository: ServiceDeskRepository,
    *,
    checkpointer: BaseCheckpointSaver[Any],
    model: BaseChatModel | None = None,
    retriever: Retriever | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    gateway: ActionGateway | None = None,
) -> ServiceDeskGraphAgent:
    model = model if model is not None else build_chat_model(settings)
    return ServiceDeskGraphAgent(
        name=AGENT_NAME,
        version=AGENT_VERSION,
        model=model,
        guard=guard_for(model, settings),
        prompt=load_prompt(settings.prompts_dir, PROMPT_NAME, prompt_version),
        executor=_executor(settings, repository, retriever, prompt_version, gateway),
        limits=AgentLimits(
            max_steps=settings.agent_max_steps, max_tool_calls=settings.agent_max_tool_calls
        ),
        checkpointer=checkpointer,
    )
