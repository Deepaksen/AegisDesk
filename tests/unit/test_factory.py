"""Model construction. No network calls happen here: constructing a client is offline."""

from __future__ import annotations

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama
from pydantic import SecretStr

from aegisdesk.config import ModelProvider, Settings
from aegisdesk.llm.allowlist import ModelNotAllowedError
from aegisdesk.llm.factory import ModelConfigurationError, build_chat_model
from aegisdesk.llm.fake import ScriptedChatModel


def test_builds_fake_model_by_default() -> None:
    model = build_chat_model(Settings())
    assert isinstance(model, ScriptedChatModel)


def test_builds_anthropic_model_with_configured_parameters() -> None:
    settings = Settings(
        model_provider=ModelProvider.ANTHROPIC,
        model_name="claude-haiku-4-5-20251001",
        model_temperature=0.2,
        model_max_tokens=512,
        model_timeout_seconds=30,
        anthropic_api_key=SecretStr("sk-ant-dummy"),
    )

    model = build_chat_model(settings)

    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-haiku-4-5-20251001"
    assert model.temperature == 0.2
    assert model.max_tokens == 512
    assert model.default_request_timeout == 30


def test_anthropic_without_api_key_fails_clearly() -> None:
    settings = Settings(
        model_provider=ModelProvider.ANTHROPIC, model_name="claude-haiku-4-5-20251001"
    )
    with pytest.raises(ModelConfigurationError, match="ANTHROPIC_API_KEY"):
        build_chat_model(settings)


def test_builds_ollama_model_with_configured_parameters() -> None:
    settings = Settings(
        model_provider=ModelProvider.OLLAMA,
        model_name="qwen2.5:7b",
        model_temperature=0.5,
        model_max_tokens=300,
        ollama_base_url="http://ollama:11434",
    )

    model = build_chat_model(settings)

    assert isinstance(model, ChatOllama)
    assert model.model == "qwen2.5:7b"
    assert model.temperature == 0.5
    assert model.num_predict == 300
    assert model.base_url == "http://ollama:11434"


def test_allowlist_is_checked_before_construction() -> None:
    settings = Settings(
        model_provider=ModelProvider.ANTHROPIC,
        model_name="some-unapproved-model",
        anthropic_api_key=SecretStr("sk-ant-dummy"),
    )
    with pytest.raises(ModelNotAllowedError):
        build_chat_model(settings)
