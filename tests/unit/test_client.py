from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.messages.ai import UsageMetadata

from aegisdesk.config import ModelProvider
from aegisdesk.llm.client import LLMClient
from aegisdesk.llm.fake import ScriptedChatModel
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.prompts.loader import Prompt


def _client(model: ScriptedChatModel) -> LLMClient:
    return LLMClient(model, provider=ModelProvider.FAKE, model_name="fake-scripted")


def test_messages_are_system_then_history_then_user(assistant_prompt: Prompt) -> None:
    history = [HumanMessage("earlier question"), AIMessage("earlier answer")]

    messages = LLMClient.build_messages(assistant_prompt, "new question", history)

    assert isinstance(messages[0], SystemMessage)
    assert messages[0].text == assistant_prompt.system
    assert messages[1:3] == history
    assert messages[-1] == HumanMessage("new question")


def test_chat_returns_text_usage_and_prompt_version(assistant_prompt: Prompt) -> None:
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="Restart the VPN client.",
                usage_metadata=UsageMetadata(input_tokens=40, output_tokens=5, total_tokens=45),
            )
        ]
    )

    response = _client(model).chat(assistant_prompt, "VPN broken")

    assert response.text == "Restart the VPN client."
    meta = response.metadata
    assert meta.usage == TokenUsage(input_tokens=40, output_tokens=5)
    assert (meta.provider, meta.model) == ("fake", "fake-scripted")
    assert (meta.prompt_name, meta.prompt_version) == ("assistant", "v1")
    assert meta.latency_ms >= 0


def test_model_receives_exactly_the_built_messages(assistant_prompt: Prompt) -> None:
    model = ScriptedChatModel()

    _client(model).chat(assistant_prompt, "hello")

    assert len(model.calls) == 1
    assert model.calls[0] == LLMClient.build_messages(assistant_prompt, "hello")


def test_fake_model_estimates_usage_when_script_has_none(assistant_prompt: Prompt) -> None:
    model = ScriptedChatModel(responses=[AIMessage(content="one two three")])

    response = _client(model).chat(assistant_prompt, "hi")

    assert response.metadata.usage.output_tokens == 3
    assert response.metadata.usage.input_tokens > 0


def test_scripted_responses_are_returned_in_order(assistant_prompt: Prompt) -> None:
    model = ScriptedChatModel(responses=[AIMessage(content="first"), AIMessage(content="second")])
    client = _client(model)

    assert client.chat(assistant_prompt, "a").text == "first"
    assert client.chat(assistant_prompt, "b").text == "second"
    # Script exhausted: falls back to deterministic demo behaviour.
    assert client.chat(assistant_prompt, "c").text == "[fake model] You said: c"
