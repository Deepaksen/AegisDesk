from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from aegisdesk.config import PROJECT_ROOT, Settings, get_settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.prompts.loader import Prompt, load_prompt
from aegisdesk.rag.embeddings import HashingEmbedder
from aegisdesk.rag.ingestion.pipeline import ingest_directory
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.rag.store.memory import InMemoryVectorStore

TODAY = date(2026, 9, 30)

# Env vars that would otherwise leak from a developer's shell or .env into tests.
_MODEL_ENV_VARS = (
    "AEGIS_ENV",
    "MODEL_PROVIDER",
    "MODEL_NAME",
    "MODEL_TEMPERATURE",
    "MODEL_MAX_TOKENS",
    "MODEL_TIMEOUT_SECONDS",
    "MODEL_MAX_RETRIES",
    "ANTHROPIC_API_KEY",
    "OLLAMA_BASE_URL",
    "AGENT_MAX_STEPS",
    "AGENT_MAX_TOOL_CALLS",
    "AGENT_MAX_HANDOFFS",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "VECTOR_STORE",
    "DATABASE_URL",
    "RAG_TOP_K",
    "RAG_MIN_SCORE",
)


@pytest.fixture(autouse=True)
def _isolate_env(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[None]:
    # Never write conversation checkpoints into the working tree during tests.
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "checkpoints.sqlite"))
    if request.node.get_closest_marker("live") is None:
        for name in _MODEL_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        # Ignore any local .env file for deterministic tests.
        monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def triage_prompt() -> Prompt:
    return load_prompt(PROJECT_ROOT / "prompts", "triage", "v1")


@pytest.fixture
def assistant_prompt() -> Prompt:
    return load_prompt(PROJECT_ROOT / "prompts", "assistant", "v1")


@pytest.fixture
def repository() -> ServiceDeskRepository:
    """A fresh in-memory repository per test, so writes never leak between tests.

    "Today" is pinned so access-expiry rules give the same answer on any date.
    """
    return ServiceDeskRepository.from_seed(PROJECT_ROOT / "data" / "seed", today=lambda: TODAY)


@pytest.fixture
def aisha(repository: ServiceDeskRepository) -> UserContext:
    """E1004, a finance employee (the spec's example user)."""
    return authenticate(repository, "E1004")


@pytest.fixture(scope="session")
def _knowledge_index() -> InMemoryVectorStore:
    store = InMemoryVectorStore()
    ingest_directory(PROJECT_ROOT / "data" / "documents", HashingEmbedder(), store)
    return store


@pytest.fixture
def retriever(_knowledge_index: InMemoryVectorStore) -> Retriever:
    """Retriever over the real corpus with the offline hashing embedder (read-only, shared)."""
    return Retriever(HashingEmbedder(), _knowledge_index, top_k=4)
