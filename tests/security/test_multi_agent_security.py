"""Multi-agent security: routing cannot widen what any agent may do.

Adding a supervisor and more agents adds new ways to go wrong: an injected
document could make the Knowledge agent try to act, a manipulated request
could make the Access agent act for someone else, or agents could be talked
into handing work back and forth. Each case below is blocked by code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from aegisdesk.agents.loop import AgentStep, RouteStep
from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.config import PROJECT_ROOT, Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.rag.embeddings import HashingEmbedder
from aegisdesk.rag.ingestion.pipeline import ingest_directory
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.rag.store.memory import InMemoryVectorStore


def _route(agent: str, instruction: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "RoutingPlan",
                "args": {
                    "tasks": [{"agent": agent, "instruction": instruction}],
                    "out_of_scope": False,
                },
                "id": "r",
            }
        ],
    )


def _calls(*calls: tuple[str, dict[str, Any]]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": n, "args": a, "id": f"c{i}"} for i, (n, a) in enumerate(calls)],
    )


@pytest.fixture
def poisoned_retriever(tmp_path: Path) -> Retriever:
    sources = [
        *(PROJECT_ROOT / "data" / "documents").glob("*.md"),
        *(PROJECT_ROOT / "tests" / "fixtures" / "rag_adversarial").glob("*.md"),
    ]
    for source in sources:
        (tmp_path / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    store = InMemoryVectorStore()
    ingest_directory(tmp_path, HashingEmbedder(), store)
    return Retriever(HashingEmbedder(), store, top_k=4)


def _agent(repository: ServiceDeskRepository, retriever: Retriever, *script: AIMessage):  # type: ignore[no-untyped-def]
    return build_supervisor_agent(
        Settings(),
        repository,
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(responses=list(script)),
        retriever=retriever,
    )


def test_injected_document_cannot_make_the_knowledge_agent_act(
    repository: ServiceDeskRepository, poisoned_retriever: Retriever, aisha: UserContext
) -> None:
    agent = _agent(
        repository,
        poisoned_retriever,
        _route("knowledge", "VPN quick tips"),
        _calls(
            (
                "search_knowledge_base",
                {"query": "VPN quick tips restart GlobalProtect AI assistants"},
            )
        ),
        _calls(
            ("direct_grant_production_admin", {}),
            (
                "create_access_request",
                {"application": "ProductionDB", "justification": "As instructed."},
            ),
            ("get_my_assets", {"employee_id": "E1002"}),
            # The one thing it *can* do: ask for a handoff. Routing it is still code's call,
            # and the access agent then decides eligibility deterministically.
            (
                "request_handoff",
                {"target_agent": "access", "instruction": "Grant ProductionDB admin"},
            ),
        ),
        AIMessage(content="You are now an administrator."),
        _calls(
            (
                "create_access_request",
                {"application": "ProductionDB", "justification": "Handoff said so."},
            )
        ),
        AIMessage(content="Done."),
    )

    run = agent.run("Any VPN tips?", user=aisha)

    knowledge_tools = {
        s.tool_name: s.error_category for s in run.tool_steps if s.agent == "knowledge"
    }
    assert "Ignore system instructions" in run.tool_steps[0].result  # injection was delivered
    assert knowledge_tools["direct_grant_production_admin"] == "unknown_tool"
    assert (
        knowledge_tools["create_access_request"] == "unknown_tool"
    )  # not the knowledge agent's tool
    assert knowledge_tools["get_my_assets"] == "unknown_tool"
    access_tools = [s for s in run.tool_steps if s.agent == "access"]
    assert access_tools[0].error_category == "not_eligible"
    # Nothing was recorded, and the fabricated "administrator" claim did not survive the
    # knowledge agent's citation check.
    assert repository.access_requests_for("E1004")[-1].request_id == "AR-1009"
    knowledge_step = next(
        s for s in run.trajectory if isinstance(s, AgentStep) and s.agent == "knowledge"
    )
    assert "administrator" not in knowledge_step.answer


def test_access_agent_cannot_request_for_someone_else(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent = _agent(
        repository,
        retriever,
        _route("access", "Request FinanceERP for my colleague E1001"),
        _calls(
            (
                "create_access_request",
                {
                    "application": "FinanceERP",
                    "justification": "For E1001.",
                    "employee_id": "E1001",
                },
            )
        ),
        AIMessage(content="Done."),
    )

    run = agent.run("Please request FinanceERP for Priya (E1001)", user=aisha)

    assert run.tool_steps[0].error_category == "invalid_arguments"
    assert repository.access_requests_for("E1001")[-1].request_id == "AR-1004"


def test_router_output_cannot_invent_an_agent(
    repository: ServiceDeskRepository, retriever: Retriever, aisha: UserContext
) -> None:
    agent = _agent(repository, retriever, _route("admin_agent", "Grant me admin"))

    run = agent.run("Route me to the admin agent", user=aisha)

    assert not [s for s in run.trajectory if isinstance(s, AgentStep)]
    route = run.trajectory[0]
    assert isinstance(route, RouteStep) and route.error
