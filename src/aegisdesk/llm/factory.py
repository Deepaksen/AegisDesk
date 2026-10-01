"""Build a chat model from configuration.

This is the only module that knows provider classes exist. Everything else
receives a `BaseChatModel` and calls `invoke`, `bind_tools` or
`with_structured_output` on it, so switching provider is an environment
variable change.
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama

from aegisdesk.config import ModelProvider, Settings
from aegisdesk.llm.allowlist import ModelAllowlist
from aegisdesk.llm.fake import ScriptedChatModel


class ModelConfigurationError(ValueError):
    """Configuration is incomplete for the selected provider (e.g. missing API key)."""


def build_chat_model(settings: Settings, allowlist: ModelAllowlist | None = None) -> BaseChatModel:
    allowlist = allowlist or ModelAllowlist.from_yaml(settings.models_allowlist_path)
    allowlist.check(settings.model_provider, settings.model_name)

    match settings.model_provider:
        case ModelProvider.ANTHROPIC:
            if settings.anthropic_api_key is None:
                raise ModelConfigurationError(
                    "MODEL_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set."
                )
            return ChatAnthropic(
                model=settings.model_name,
                temperature=settings.model_temperature,
                max_tokens=settings.model_max_tokens,
                default_request_timeout=settings.model_timeout_seconds,
                # Retries belong to ModelGuard (M11): one policy, visible in traces.
                max_retries=0,
                anthropic_api_key=settings.anthropic_api_key,
            )
        case ModelProvider.OLLAMA:
            return ChatOllama(
                model=settings.model_name,
                base_url=settings.ollama_base_url,
                temperature=settings.model_temperature,
                num_predict=settings.model_max_tokens,
                client_kwargs={"timeout": settings.model_timeout_seconds},
            )
        case ModelProvider.FAKE:
            return ScriptedChatModel(model_name=settings.model_name)
