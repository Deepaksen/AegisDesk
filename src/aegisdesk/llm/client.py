"""A thin, explicit wrapper around a chat model.

`LLMClient` does three things a raw `model.invoke(...)` call does not:

1. Builds the message list from a *versioned* prompt, so every result can say
   which prompt produced it.
2. Measures latency and token usage for every call.
3. Turns structured-output failures into a typed error instead of `None`.

It deliberately does not retry, cache, route or decide anything. Those are
application responsibilities that later milestones add explicitly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from aegisdesk.config import ModelProvider
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.prompts.loader import Prompt


class StructuredOutputError(RuntimeError):
    """The model's answer could not be turned into the requested schema."""

    def __init__(self, message: str, raw: AIMessage | None) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class CallMetadata:
    provider: str
    model: str
    prompt_name: str
    prompt_version: str
    usage: TokenUsage
    latency_ms: float


@dataclass(frozen=True)
class ChatResponse:
    text: str
    metadata: CallMetadata


@dataclass(frozen=True)
class StructuredResponse[T: BaseModel]:
    value: T
    metadata: CallMetadata


class LLMClient:
    def __init__(self, model: BaseChatModel, provider: ModelProvider, model_name: str) -> None:
        self._model = model
        self._provider = provider
        self._model_name = model_name

    @staticmethod
    def build_messages(
        prompt: Prompt, user_input: str, history: list[BaseMessage] | None = None
    ) -> list[BaseMessage]:
        """System instructions first, then prior turns, then the new user message."""
        return [SystemMessage(content=prompt.system), *(history or []), HumanMessage(user_input)]

    def chat(
        self, prompt: Prompt, user_input: str, history: list[BaseMessage] | None = None
    ) -> ChatResponse:
        messages = self.build_messages(prompt, user_input, history)
        started = time.perf_counter()
        response = self._model.invoke(messages)
        latency_ms = (time.perf_counter() - started) * 1000

        return ChatResponse(
            text=response.text,
            metadata=self._metadata(prompt, TokenUsage.from_message(response), latency_ms),
        )

    def structured[T: BaseModel](
        self,
        prompt: Prompt,
        user_input: str,
        schema: type[T],
        history: list[BaseMessage] | None = None,
    ) -> StructuredResponse[T]:
        messages = self.build_messages(prompt, user_input, history)
        # include_raw=True keeps the raw AIMessage (and so its token usage) and
        # returns parse failures as data instead of raising inside LangChain.
        runnable = self._model.with_structured_output(schema, include_raw=True)

        started = time.perf_counter()
        result = cast(dict[str, Any], runnable.invoke(messages))
        latency_ms = (time.perf_counter() - started) * 1000

        raw = cast(AIMessage | None, result.get("raw"))
        parsed = result.get("parsed")
        error = result.get("parsing_error")
        if error is not None or not isinstance(parsed, schema):
            reason = error or "model did not return the requested structure"
            raise StructuredOutputError(
                f"Could not parse model output as {schema.__name__}: {reason}", raw=raw
            )

        usage = TokenUsage.from_message(raw) if raw is not None else TokenUsage()
        return StructuredResponse(value=parsed, metadata=self._metadata(prompt, usage, latency_ms))

    def _metadata(self, prompt: Prompt, usage: TokenUsage, latency_ms: float) -> CallMetadata:
        return CallMetadata(
            provider=self._provider.value,
            model=self._model_name,
            prompt_name=prompt.name,
            prompt_version=prompt.version,
            usage=usage,
            latency_ms=round(latency_ms, 2),
        )
