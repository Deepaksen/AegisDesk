"""Calls to real providers. Excluded from CI; run with `uv run pytest -m live`.

Anthropic: set ANTHROPIC_API_KEY (optionally MODEL_NAME).
Ollama:    run `ollama serve` and `ollama pull llama3.2` (optionally OLLAMA_BASE_URL, MODEL_NAME).

These tests assert shape, not wording: real models are not deterministic.
"""

from __future__ import annotations

import os
import urllib.request

import pytest

from aegisdesk.config import ModelProvider, Settings
from aegisdesk.llm.client import LLMClient
from aegisdesk.llm.factory import build_chat_model
from aegisdesk.prompts.loader import Prompt
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
