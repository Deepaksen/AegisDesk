from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aegisdesk.config import PROJECT_ROOT, Settings, get_settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import UserContext, authenticate
from aegisdesk.prompts.loader import Prompt, load_prompt

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
    """A fresh in-memory repository per test, so writes never leak between tests."""
    return ServiceDeskRepository.from_seed(PROJECT_ROOT / "data" / "seed")


@pytest.fixture
def aisha(repository: ServiceDeskRepository) -> UserContext:
    """E1004, a finance employee (the spec's example user)."""
    return authenticate(repository, "E1004")
