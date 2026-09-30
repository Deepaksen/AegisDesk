from __future__ import annotations

from pathlib import Path

import pytest

from aegisdesk.config import PROJECT_ROOT, ModelProvider
from aegisdesk.llm.allowlist import ModelAllowlist, ModelNotAllowedError


@pytest.fixture
def allowlist() -> ModelAllowlist:
    return ModelAllowlist.from_yaml(PROJECT_ROOT / "config" / "models.yaml")


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        (ModelProvider.ANTHROPIC, "claude-haiku-4-5-20251001"),
        (ModelProvider.ANTHROPIC, "claude-sonnet-5-5"),
        (ModelProvider.OLLAMA, "llama3.2"),
        (ModelProvider.OLLAMA, "qwen2.5:7b"),
        (ModelProvider.FAKE, "fake-scripted"),
    ],
)
def test_repository_allowlist_accepts_configured_models(
    allowlist: ModelAllowlist, provider: ModelProvider, model: str
) -> None:
    allowlist.check(provider, model)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        (ModelProvider.ANTHROPIC, "claude-unknown"),
        # Right model, wrong provider.
        (ModelProvider.OLLAMA, "claude-sonnet-5-5"),
        (ModelProvider.FAKE, "llama3.2"),
    ],
)
def test_rejects_models_not_listed_for_provider(
    allowlist: ModelAllowlist, provider: ModelProvider, model: str
) -> None:
    with pytest.raises(ModelNotAllowedError, match="not allowlisted"):
        allowlist.check(provider, model)


def test_unknown_provider_in_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("providers:\n  someprovider:\n    - m1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="someprovider"):
        ModelAllowlist.from_yaml(path)


def test_empty_provider_allows_nothing(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("providers:\n  ollama:\n", encoding="utf-8")
    allowlist = ModelAllowlist.from_yaml(path)
    assert not allowlist.is_allowed(ModelProvider.OLLAMA, "llama3.2")
