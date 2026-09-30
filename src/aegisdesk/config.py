"""Application configuration.

All runtime configuration comes from environment variables (or a local `.env`
file) and is validated by Pydantic before anything else runs. Two rules:

* Model choice is configuration, not code. Swapping Anthropic for a local
  Ollama model must not require touching agent or graph logic.
* Secrets are held as `SecretStr` so they never appear in `repr()`, logs,
  prompts or serialized state by accident.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr

__all__ = ["TelemetryExporter"]  # re-exported for settings users
from pydantic_settings import BaseSettings, SettingsConfigDict

from aegisdesk.observability.setup import TelemetryExporter

# Repository root: src/aegisdesk/config.py -> parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ModelProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    # Deterministic, offline model used by tests and demos without credentials.
    FAKE = "fake"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class EmbeddingProvider(StrEnum):
    HASH = "hash"  # deterministic, offline; tests and CI
    OLLAMA = "ollama"


class VectorStoreKind(StrEnum):
    MEMORY = "memory"
    PGVECTOR = "pgvector"


class AuditStoreKind(StrEnum):
    MEMORY = "memory"  # per process; tests and demos
    POSTGRES = "postgres"  # append-only audit_events table (migration 0002)


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"  # one JSON object per line, with trace_id / request_id / thread_id


class DataStoreKind(StrEnum):
    MEMORY = "memory"  # seeded per process; approvals do not survive a restart
    POSTGRES = "postgres"  # access requests, approvals, granted access (migration 0003)


class CheckpointStoreKind(StrEnum):
    SQLITE = "sqlite"  # a local file (CHECKPOINT_DB_PATH)
    POSTGRES = "postgres"  # LangGraph's Postgres checkpointer on DATABASE_URL


class ToolTransport(StrEnum):
    # Tools run in the agent's own process (Milestones 1-4).
    LOCAL = "local"
    # Enterprise tools behind the two MCP servers, connected in-process.
    # Full MCP protocol and token checks, no network; tests and demos.
    MCP_INPROCESS = "mcp_inprocess"
    # Enterprise tools behind MCP servers over Streamable HTTP (`aegisdesk mcp serve`).
    MCP_HTTP = "mcp_http"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    aegis_env: Environment = Environment.DEVELOPMENT

    model_provider: ModelProvider = ModelProvider.FAKE
    model_name: str = "fake-scripted"
    model_temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    model_max_tokens: int = Field(default=1024, gt=0, le=64_000)
    model_timeout_seconds: float = Field(default=60.0, gt=0)
    # Transport-level retries for read-only model calls. Kept small on purpose.
    model_max_retries: int = Field(default=2, ge=0, le=5)

    anthropic_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434"

    # Hard limits on one agent request, so a confused model cannot loop forever.
    agent_max_steps: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls: int = Field(default=8, ge=0, le=50)
    # Extra specialist tasks the supervisor accepts from handoffs in one turn.
    agent_max_handoffs: int = Field(default=2, ge=0, le=5)

    models_allowlist_path: Path = PROJECT_ROOT / "config" / "models.yaml"
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    seed_data_dir: Path = PROJECT_ROOT / "data" / "seed"
    # Where LangGraph saves conversation threads (git-ignored).
    checkpoint_db_path: Path = PROJECT_ROOT / ".aegisdesk" / "checkpoints.sqlite"

    # Knowledge base (RAG)
    documents_dir: Path = PROJECT_ROOT / "data" / "documents"
    embedding_provider: EmbeddingProvider = EmbeddingProvider.HASH
    embedding_model: str = "nomic-embed-text"
    vector_store: VectorStoreKind = VectorStoreKind.MEMORY
    database_url: str = "postgresql+psycopg://aegisdesk:aegisdesk@localhost:5432/aegisdesk"
    rag_top_k: int = Field(default=4, ge=1, le=20)
    # Below this cosine similarity a chunk is not treated as evidence.
    # Unset: use the embedder's calibrated default.
    rag_min_score: float | None = Field(default=None, ge=-1.0, le=1.0)

    # Tools over MCP (Milestone 5)
    tool_transport: ToolTransport = ToolTransport.LOCAL
    # Signs the delegation tokens the host sends to MCP servers. Required for
    # mcp_http (host and servers must share it); mcp_inprocess generates a
    # throwaway key when unset.
    mcp_token_secret: SecretStr | None = None
    mcp_read_url: str = "http://127.0.0.1:8765/read/mcp"
    mcp_action_url: str = "http://127.0.0.1:8765/action/mcp"
    mcp_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    # Governance (Milestone 6)
    policy_path: Path = PROJECT_ROOT / "config" / "policy.yaml"
    audit_store: AuditStoreKind = AuditStoreKind.MEMORY

    # Human approval (Milestone 7)
    data_store: DataStoreKind = DataStoreKind.MEMORY
    checkpoint_store: CheckpointStoreKind = CheckpointStoreKind.SQLITE
    approval_ttl_hours: int = Field(default=168, ge=1, le=24 * 90)

    # Observability (Milestone 8). OTLP endpoint and headers use the standard
    # OTEL_EXPORTER_OTLP_* variables; LangSmith uses LANGSMITH_TRACING/_API_KEY/_PROJECT.
    telemetry_exporter: TelemetryExporter = TelemetryExporter.NONE
    log_format: LogFormat = LogFormat.TEXT
    log_level: str = Field(default="WARNING", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once from the environment."""
    return Settings()
