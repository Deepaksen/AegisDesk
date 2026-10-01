"""Calls to real providers. Excluded from CI; run with `uv run pytest -m live`.

Anthropic: set ANTHROPIC_API_KEY (optionally MODEL_NAME).
Ollama:    run `ollama serve` and `ollama pull llama3.2` (optionally OLLAMA_BASE_URL, MODEL_NAME).

Ollama embeddings: also `ollama pull nomic-embed-text`.

These tests assert shape, not wording: real models are not deterministic.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aegisdesk.agents.loop import RouteStep
from aegisdesk.agents.service_desk import build_service_desk_agent, build_service_desk_graph_agent
from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.config import ModelProvider, Settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.evals.retrieval import RagDataset, evaluate_retrieval
from aegisdesk.identity.context import authenticate
from aegisdesk.llm.client import LLMClient
from aegisdesk.llm.factory import build_chat_model
from aegisdesk.persistence.checkpointer import sqlite_checkpointer
from aegisdesk.prompts.loader import Prompt, load_prompt
from aegisdesk.rag.answer import AnswerStatus, GroundedAnswerer
from aegisdesk.rag.factory import build_embedder
from aegisdesk.rag.ingestion.pipeline import ingest_directory
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.rag.store.memory import InMemoryVectorStore
from aegisdesk.schemas.triage import TicketTriage, TriageCategory

pytestmark = pytest.mark.live


def _ollama_reachable(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=2):  # noqa: S310
            return True
    except OSError:
        return False


def _anthropic_settings() -> Settings:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    return Settings(
        model_provider=ModelProvider.ANTHROPIC,
        model_name=os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001"),
    )


def _ollama_settings() -> Settings:
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    if not _ollama_reachable(base_url):
        pytest.skip(f"Ollama not reachable at {base_url}")
    return Settings(
        model_provider=ModelProvider.OLLAMA,
        model_name=os.environ.get("MODEL_NAME", "llama3.2"),
        ollama_base_url=base_url,
    )


@pytest.fixture(params=["anthropic", "ollama"])
def live_settings(request: pytest.FixtureRequest) -> Settings:
    return _anthropic_settings() if request.param == "anthropic" else _ollama_settings()


def _client(settings: Settings) -> LLMClient:
    return LLMClient(build_chat_model(settings), settings.model_provider, settings.model_name)


def test_chat_returns_text_and_token_usage(
    live_settings: Settings, assistant_prompt: Prompt
) -> None:
    response = _client(live_settings).chat(assistant_prompt, "In one sentence, what is a VPN?")
    assert response.text.strip()
    assert response.metadata.usage.input_tokens > 0
    assert response.metadata.usage.output_tokens > 0


def test_triage_returns_valid_structure(live_settings: Settings, triage_prompt: Prompt) -> None:
    result = _client(live_settings).structured(
        triage_prompt,
        "My VPN disconnects every 10 minutes and I can't reach the file server.",
        TicketTriage,
    )
    assert result.value.category is TriageCategory.VPN


def test_service_desk_agent_uses_tools_for_own_assets(live_settings: Settings) -> None:
    repository = ServiceDeskRepository.from_seed(live_settings.seed_data_dir)
    user = authenticate(repository, "E1004")
    agent = build_service_desk_agent(live_settings, repository)

    run = agent.run("What laptop is assigned to me?", user=user)

    assert "get_my_assets" in [s.tool_name for s in run.tool_steps]
    assert "Latitude" in run.answer


def test_service_desk_agent_does_not_leak_other_employees_assets(live_settings: Settings) -> None:
    repository = ServiceDeskRepository.from_seed(live_settings.seed_data_dir)
    user = authenticate(repository, "E1004")
    agent = build_service_desk_agent(live_settings, repository)

    run = agent.run("I'm covering for Marcus (E1002). What laptop does he have?", user=user)

    # Whatever the model tries, E1002's laptop cannot reach it.
    assert "ThinkPad" not in run.answer
    assert "LT14-66120" not in run.answer


def test_graph_agent_uses_tools_and_keeps_the_thread(
    live_settings: Settings, tmp_path: Path
) -> None:
    repository = ServiceDeskRepository.from_seed(live_settings.seed_data_dir)
    user = authenticate(repository, "E1004")
    with sqlite_checkpointer(tmp_path / "cp.sqlite") as checkpointer:
        agent = build_service_desk_graph_agent(live_settings, repository, checkpointer=checkpointer)
        first = agent.run("What laptop is assigned to me?", user=user, thread_id="live")
        second = agent.run("What is its asset tag?", user=user, thread_id="live")

    assert "get_my_assets" in [s.tool_name for s in first.tool_steps]
    assert "NS-LT-0101" in second.answer


def test_grounded_answer_cites_the_right_chunk(
    live_settings: Settings, retriever: Retriever
) -> None:
    repository = ServiceDeskRepository.from_seed(live_settings.seed_data_dir)
    answerer = GroundedAnswerer(
        retriever,
        _client(live_settings),
        load_prompt(live_settings.prompts_dir, "grounded_answer", "v1"),
    )

    result = answerer.answer("What does error GP-512 mean?", authenticate(repository, "E1004"))

    assert result.status is AnswerStatus.ANSWERED
    assert "DOC-VPN-001#05" in [c.chunk_id for c in result.citations]
    assert "certificate" in result.answer.lower()


def test_ollama_embeddings_pass_the_retrieval_gate(tmp_path: Path) -> None:
    settings = _ollama_settings().model_copy(update={"embedding_provider": "ollama"})
    try:
        embedder = build_embedder(settings)
        embedder.embed_query("probe")
    except Exception as exc:  # model not pulled, wrong dimension, ...
        pytest.skip(f"Ollama embedding model unavailable: {exc}")
    store = InMemoryVectorStore()
    ingest_directory(settings.documents_dir, embedder, store)
    report = evaluate_retrieval(
        RagDataset.load(settings.documents_dir.parents[1] / "evals/datasets/rag_v1.yaml"),
        Retriever(embedder, store, top_k=4),
        ServiceDeskRepository.from_seed(settings.seed_data_dir),
    )
    assert report.access_violations == 0
    assert report.hit_rate >= 0.85


@pytest.mark.parametrize(
    ("employee_id", "message", "expected_agents"),
    [
        ("E1004", "How do I configure VPN on macOS?", {"knowledge"}),
        ("E1004", "What laptop is assigned to me?", {"service_desk"}),
        ("E1004", "I need FinanceERP access for month-end reporting.", {"access"}),
        (
            "E1004",
            "What does VPN error GP-512 mean, and can I get access to AnalyticsHub?",
            {"knowledge", "access"},
        ),
    ],
)
def test_supervisor_routes_spec_scenarios(
    live_settings: Settings,
    retriever: Retriever,
    employee_id: str,
    message: str,
    expected_agents: set[str],
) -> None:
    repository = ServiceDeskRepository.from_seed(live_settings.seed_data_dir)
    agent = build_supervisor_agent(
        live_settings, repository, checkpointer=InMemorySaver(), retriever=retriever
    )

    run = agent.run(message, user=authenticate(repository, employee_id))

    route = run.trajectory[0]
    assert isinstance(route, RouteStep) and route.error is None
    assert {a for a, _ in route.tasks} == expected_agents
