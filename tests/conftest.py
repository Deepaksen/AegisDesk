from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from aegisdesk.audit.events import InMemoryAuditLog
from aegisdesk.config import PROJECT_ROOT, Settings, get_settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.governance.factory import build_gateway
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.observability import faults
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
    "TOOL_TRANSPORT",
    "MCP_TOKEN_SECRET",
    "MCP_READ_URL",
    "MCP_ACTION_URL",
    "MCP_TIMEOUT_SECONDS",
    "POLICY_PATH",
    "AUDIT_STORE",
    "DATA_STORE",
    "CHECKPOINT_STORE",
    "APPROVAL_TTL_HOURS",
    "TELEMETRY_EXPORTER",
    "LOG_FORMAT",
    "LOG_LEVEL",
    "AEGIS_FAULTS",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
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
    faults.reload()
    yield
    get_settings.cache_clear()
    faults.reload()


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


@pytest.fixture
def audit_log() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def gateway(audit_log: InMemoryAuditLog, repository: ServiceDeskRepository) -> ActionGateway:
    """The real policy (config/policy.yaml) in the default environment, auditing to memory."""
    return build_gateway(Settings(), audit=audit_log, access_store=repository.access_store)
