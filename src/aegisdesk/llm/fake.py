"""A deterministic, offline chat model.

Why this exists: normal software tests must not depend on a network, an API
key, or a model whose output changes between runs. `ScriptedChatModel`
implements the same LangChain `BaseChatModel` interface as `ChatAnthropic` and
`ChatOllama`, so the code under test cannot tell the difference.

Two modes:

* **Scripted:** pass `responses=[AIMessage(...), ...]` and they are returned
  in order. Tests use this to control exactly what "the model" says,
  including malformed output and misbehaving tool calls.
* **Demo:** with no script left, it answers deterministically, so the CLI
  runs offline. It is not intelligent and never pretends to be:
  - just after tool results: it replies with those results as text;
  - forced tool use (structured output): it calls the tool with arguments
    filled from the user's text;
  - tools offered: it picks the tool whose name and description share the
    most words with the user's message (an ID matching a parameter's pattern
    counts extra), or echoes the message if none match;
  - no tools: it echoes the user's message.

Token counts are a whitespace word count. That is enough to exercise the usage
accounting code paths, but it is not a real tokenizer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, PrivateAttr

_STOPWORDS = frozenset(
    {"what", "which", "with", "that", "this", "from", "your", "have", "been", "please", "the"}
)


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
            message = self._demo_response(
                messages, kwargs.get("tools") or [], kwargs.get("tool_choice")
            )

        if message.usage_metadata is None:
            message.usage_metadata = self._usage(messages, message)
        message.response_metadata = {**message.response_metadata, "model_name": self.model_name}
        return ChatResult(generations=[ChatGeneration(message=message)])

    # -- demo behaviour -------------------------------------------------

    def _demo_response(
        self, messages: list[BaseMessage], tools: list[dict[str, Any]], tool_choice: str | None
    ) -> AIMessage:
        if messages and isinstance(messages[-1], ToolMessage):
            results = [_message_text(m) for m in _trailing_tool_messages(messages)]
            return AIMessage(content="[fake model] Tool results: " + " ".join(results))

        user_text = next(
            (_message_text(m) for m in reversed(messages) if isinstance(m, HumanMessage)), ""
        )
        if not tools:
            return AIMessage(content=f"[fake model] You said: {user_text}")

        forced = tool_choice is not None and tool_choice not in ("auto", "none")
        function = tools[0]["function"] if forced else _best_matching_tool(tools, user_text)
        if function is None:
            return AIMessage(content=f"[fake model] You said: {user_text}")

        parameters = function.get("parameters", {})
        args = _fill_schema(parameters, parameters.get("$defs", {}), user_text)
        return AIMessage(
            content="",
            tool_calls=[{"name": function["name"], "args": args, "id": f"fake-call-{self._n()}"}],
        )

    def _n(self) -> int:
        return len(self._calls)

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


def _trailing_tool_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    trailing: list[BaseMessage] = []
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        trailing.insert(0, message)
    return trailing


def _stems(text: str) -> set[str]:
    """Crude word stems: lowercase words of 4+ letters, cut to 5 characters."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w[:5] for w in words if len(w) >= 4 and w not in _STOPWORDS}


def _best_matching_tool(tools: list[dict[str, Any]], user_text: str) -> dict[str, Any] | None:
    wanted = _stems(user_text)
    best: dict[str, Any] | None = None
    best_score = 0
    for tool in tools:
        function: dict[str, Any] = tool["function"]
        offered = _stems(function["name"].replace("_", " ") + " " + function.get("description", ""))
        score = len(wanted & offered)
        # An identifier in the text (e.g. INC-1001) that fits a parameter's pattern
        # is strong evidence for that tool.
        for prop in function.get("parameters", {}).get("properties", {}).values():
            pattern = prop.get("pattern")
            if pattern and re.search(pattern.strip("^$"), user_text, re.IGNORECASE):
                score += 2
        if score > best_score:  # ties keep the earlier tool
            best, best_score = function, score
    return best


def _fill_schema(schema: dict[str, Any], defs: dict[str, Any], user_text: str) -> dict[str, Any]:
    """Deterministically produce arguments matching a JSON schema object.

    Fields with a default use it. Enums pick the option whose description
    (written as "option: words; ...") best matches the text, else the first
    value mentioned in the text, else the first value. Lists of objects get
    one filled-in item. Strings with a `pattern` take the first
    match in the text; other strings take the text's first paragraph. Lists of
    pattern-constrained strings take every match. Booleans are False and
    numbers take their minimum (or 0).
    """
    lowered = user_text.lower()
    args: dict[str, Any] = {}
    for name, prop in schema.get("properties", {}).items():
        resolved = _resolve(prop, defs)
        if "default" in prop or "default" in resolved:
            args[name] = prop.get("default", resolved.get("default"))
        elif "enum" in resolved:
            options = [str(v) for v in resolved["enum"]]
            description = prop.get("description") or resolved.get("description") or ""
            args[name] = _choose_option(options, description, user_text) or next(
                (o for o in options if o.lower() in lowered), options[0]
            )
        elif resolved.get("type") == "boolean":
            args[name] = False
        elif resolved.get("type") in ("integer", "number"):
            args[name] = resolved.get("minimum", 0)
        elif resolved.get("type") == "array":
            # A list of pattern-constrained strings (e.g. citation IDs) gets every
            # distinct match in the text; any other list is empty.
            # A list of objects gets one filled-in item.
            items = _resolve(resolved.get("items", {}), defs)
            if items.get("type") == "object" or "properties" in items:
                args[name] = [_fill_schema(items, defs, user_text)]
                continue
            item_pattern = items.get("pattern")
            matches = re.findall(item_pattern.strip("^$"), user_text) if item_pattern else []
            args[name] = list(dict.fromkeys(matches))
        elif "pattern" in resolved:
            match = re.search(resolved["pattern"].strip("^$"), user_text, re.IGNORECASE)
            args[name] = match.group(0) if match else user_text
        else:
            # The first paragraph only, so an attached context block is not echoed.
            first_paragraph = user_text.split("\n\n", 1)[0]
            args[name] = first_paragraph[: resolved.get("maxLength", 200)]
    return args


def _choose_option(options: list[str], description: str, user_text: str) -> str | None:
    """Pick the enum option whose description ("option: words; other: words") best matches."""
    wanted = _stems(user_text)
    best, best_score = None, 0
    for option in options:
        match = re.search(rf"\b{re.escape(option)}:\s*(.*?)(?=;\s*\w+:|$)", description, re.S)
        if match:
            score = len(wanted & _stems(match.group(1)))
            if score > best_score:
                best, best_score = option, score
    return best


def _resolve(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = prop.get("$ref")
    if ref is None and prop.get("allOf"):
        ref = prop["allOf"][0].get("$ref")
    if isinstance(ref, str):
        resolved: dict[str, Any] = defs.get(ref.rsplit("/", 1)[-1], {})
        return resolved
    return prop
