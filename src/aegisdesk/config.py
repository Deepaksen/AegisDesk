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
from pydantic_settings import BaseSettings, SettingsConfigDict

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once from the environment."""
    return Settings()
