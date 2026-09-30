"""The Service Desk agent: a tool-calling agent with exactly the Service Desk tools."""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from aegisdesk.agents.loop import AgentLimits, ToolCallingAgent
from aegisdesk.config import Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.llm.factory import build_chat_model
from aegisdesk.prompts.loader import load_prompt
from aegisdesk.tools.executor import ToolExecutor
from aegisdesk.tools.service_desk import build_service_desk_tools

AGENT_NAME = "service_desk"
AGENT_VERSION = "0.1.0"
PROMPT_NAME = "service_desk"


def build_service_desk_agent(
    settings: Settings,
    repository: ServiceDeskRepository,
    *,
    model: BaseChatModel | None = None,
    prompt_version: str = "v1",
) -> ToolCallingAgent:
    return ToolCallingAgent(
        name=AGENT_NAME,
        version=AGENT_VERSION,
        model=model if model is not None else build_chat_model(settings),
        prompt=load_prompt(settings.prompts_dir, PROMPT_NAME, prompt_version),
        executor=ToolExecutor(build_service_desk_tools(repository)),
        limits=AgentLimits(
            max_steps=settings.agent_max_steps, max_tool_calls=settings.agent_max_tool_calls
        ),
    )
