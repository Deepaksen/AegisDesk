"""A deterministic, offline chat model.

Why this exists: normal software tests must not depend on a network, an API
key, or a model whose output changes between runs. `ScriptedChatModel`
implements the same LangChain `BaseChatModel` interface as `ChatAnthropic` and
`ChatOllama`, so the code under test cannot tell the difference.

Two modes:

* **Scripted** - pass `responses=[AIMessage(...), ...]` and they are returned
  in order. Tests use this to control exactly what "the model" says,
  including malformed output.
* **Demo** - with no script, it answers deterministically: plain chat echoes
  the last user message, and when tools are bound (which is how structured
  output works) it fills the tool's JSON schema from the user's text. This
  lets `aegisdesk triage` run offline. It is not intelligent and never
  pretends to be.

Token counts are a whitespace word count - good enough to exercise the usage
accounting code paths, not a real tokenizer.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, PrivateAttr


def _word_count(text: str) -> int:
    return len(text.split())


def _message_text(message: BaseMessage) -> str:
    # `.text` joins the text blocks of a message and ignores tool-use blocks.
    return message.text


class ScriptedChatModel(BaseChatModel):
    """Chat model that returns scripted or deterministic demo responses."""

    model_name: str = "fake-scripted"
    responses: list[AIMessage] = Field(default_factory=list)

    _cursor: int = PrivateAttr(default=0)
    # Every list of messages the model received; handy for assertions in tests.
    _calls: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    @property
    def calls(self) -> list[list[BaseMessage]]:
        return self._calls

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._calls.append(list(messages))

        if self._cursor < len(self.responses):
            message = self.responses[self._cursor].model_copy()
            self._cursor += 1
        else:
            message = self._demo_response(messages, kwargs.get("tools") or [])

        if message.usage_metadata is None:
            message.usage_metadata = self._usage(messages, message)
        message.response_metadata = {**message.response_metadata, "model_name": self.model_name}
        return ChatResult(generations=[ChatGeneration(message=message)])

    # -- demo behaviour -------------------------------------------------

    def _demo_response(self, messages: list[BaseMessage], tools: list[dict[str, Any]]) -> AIMessage:
        user_text = next(
            (_message_text(m) for m in reversed(messages) if isinstance(m, HumanMessage)), ""
        )
        if not tools:
            return AIMessage(content=f"[fake model] You said: {user_text}")

        function = tools[0]["function"]
        parameters = function.get("parameters", {})
        args = _fill_schema(parameters, parameters.get("$defs", {}), user_text)
        return AIMessage(
            content="",
            tool_calls=[{"name": function["name"], "args": args, "id": "fake-call-1"}],
        )

    @staticmethod
    def _usage(messages: list[BaseMessage], output: AIMessage) -> UsageMetadata:
        input_tokens = sum(_word_count(_message_text(m)) for m in messages)
        output_text = _message_text(output) + "".join(
            json.dumps(call["args"]) for call in output.tool_calls
        )
        output_tokens = _word_count(output_text)
        return UsageMetadata(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )


def _fill_schema(schema: dict[str, Any], defs: dict[str, Any], user_text: str) -> dict[str, Any]:
    """Deterministically produce arguments matching a JSON schema object.

    Enums pick the first value mentioned in the user's text (else the first
    value), strings get the user's text, booleans are False, numbers are 0.
    """
    lowered = user_text.lower()
    args: dict[str, Any] = {}
    for name, prop in schema.get("properties", {}).items():
        resolved = _resolve(prop, defs)
        if "enum" in resolved:
            options = [str(v) for v in resolved["enum"]]
            args[name] = next((o for o in options if o.lower() in lowered), options[0])
        elif resolved.get("type") == "boolean":
            args[name] = False
        elif resolved.get("type") in ("integer", "number"):
            args[name] = 0
        elif resolved.get("type") == "array":
            args[name] = []
        else:
            args[name] = user_text[:200]
    return args


def _resolve(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = prop.get("$ref")
    if ref is None and prop.get("allOf"):
        ref = prop["allOf"][0].get("$ref")
    if isinstance(ref, str):
        resolved: dict[str, Any] = defs.get(ref.rsplit("/", 1)[-1], {})
        return resolved
    return prop
