from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegisdesk.config import Environment, ModelProvider, Settings

SECRET = "sk-ant-test-not-a-real-key-123"


def test_defaults_use_offline_fake_model() -> None:
    settings = Settings()
    assert settings.model_provider is ModelProvider.FAKE
    assert settings.model_name == "fake-scripted"
    assert settings.model_temperature == 0.0
    assert settings.aegis_env is Environment.DEVELOPMENT
    assert settings.anthropic_api_key is None


def test_reads_model_configuration_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_NAME", "llama3.2")
    monkeypatch.setenv("MODEL_TEMPERATURE", "0.7")
    monkeypatch.setenv("MODEL_MAX_TOKENS", "256")

    settings = Settings()

    assert settings.model_provider is ModelProvider.OLLAMA
    assert settings.model_name == "llama3.2"
    assert settings.model_temperature == 0.7
    assert settings.model_max_tokens == 256


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MODEL_PROVIDER", "openai"),
        ("MODEL_TEMPERATURE", "1.5"),
        ("MODEL_MAX_TOKENS", "0"),
        ("MODEL_TIMEOUT_SECONDS", "-1"),
    ],
)
def test_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_api_key_is_never_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    settings = Settings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == SECRET
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert SECRET not in settings.model_dump_json()
