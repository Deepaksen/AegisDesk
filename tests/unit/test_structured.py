"""Structured output: the model's answer becomes validated data, or a typed error."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata

from aegisdesk.config import ModelProvider
from aegisdesk.llm.client import LLMClient, StructuredOutputError
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.prompts.loader import Prompt
from aegisdesk.schemas.triage import TicketTriage, TriageCategory, Urgency


def _tool_call(args: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "TicketTriage", "args": args, "id": "call-1"}],
        usage_metadata=UsageMetadata(input_tokens=100, output_tokens=20, total_tokens=120),
    )


def _client(model: ScriptedChatModel) -> LLMClient:
    return LLMClient(model, provider=ModelProvider.FAKE, model_name="fake-scripted")


VALID = {
    "category": "vpn",
    "urgency": "medium",
    "summary": "VPN disconnects every ten minutes.",
    "needs_human": False,
}


def test_valid_tool_call_is_parsed_into_schema(triage_prompt: Prompt) -> None:
    model = ScriptedChatModel(responses=[_tool_call(VALID)])

    result = _client(model).structured(triage_prompt, "My VPN drops", TicketTriage)

    assert result.value == TicketTriage(
        category=TriageCategory.VPN,
        urgency=Urgency.MEDIUM,
        summary="VPN disconnects every ten minutes.",
        needs_human=False,
    )
    # Usage comes from the raw message, which include_raw=True preserves.
    assert result.metadata.usage == TokenUsage(input_tokens=100, output_tokens=20)
    assert result.metadata.prompt_name == "triage"


@pytest.mark.parametrize(
    "bad_args",
    [
        {**VALID, "category": "printer"},  # not an allowed enum value
        {**VALID, "urgency": "critical"},
        {**VALID, "summary": ""},  # violates min_length
        {k: v for k, v in VALID.items() if k != "needs_human"},  # missing field
    ],
)
def test_invalid_structure_raises_typed_error(
    triage_prompt: Prompt, bad_args: dict[str, Any]
) -> None:
    model = ScriptedChatModel(responses=[_tool_call(bad_args)])

    with pytest.raises(StructuredOutputError) as excinfo:
        _client(model).structured(triage_prompt, "My VPN drops", TicketTriage)

    # The raw response is kept for debugging (and, later, for traces).
    assert excinfo.value.raw is not None
    assert excinfo.value.raw.tool_calls[0]["args"] == bad_args


def test_plain_text_instead_of_structure_raises(triage_prompt: Prompt) -> None:
    model = ScriptedChatModel(responses=[AIMessage(content="It's a VPN problem, probably.")])

    with pytest.raises(StructuredOutputError, match="did not return the requested structure"):
        _client(model).structured(triage_prompt, "My VPN drops", TicketTriage)


def test_structured_output_binds_schema_as_a_forced_tool(triage_prompt: Prompt) -> None:
    model = ScriptedChatModel()

    result = _client(model).structured(triage_prompt, "The VPN keeps failing", TicketTriage)

    # Demo mode fills the schema deterministically from the text.
    assert result.value.category is TriageCategory.VPN
    assert result.value.summary == "The VPN keeps failing"
