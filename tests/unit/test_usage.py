from __future__ import annotations

from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata

from aegisdesk.llm.usage import TokenUsage


def test_reads_usage_metadata_from_message() -> None:
    message = AIMessage(
        content="hi",
        usage_metadata=UsageMetadata(input_tokens=12, output_tokens=3, total_tokens=15),
    )
    usage = TokenUsage.from_message(message)
    assert usage == TokenUsage(input_tokens=12, output_tokens=3)
    assert usage.total_tokens == 15


def test_missing_usage_counts_as_zero() -> None:
    assert TokenUsage.from_message(AIMessage(content="hi")) == TokenUsage()


def test_usage_can_be_summed_across_calls() -> None:
    total = TokenUsage(10, 2) + TokenUsage(5, 1) + TokenUsage()
    assert total == TokenUsage(input_tokens=15, output_tokens=3)
    assert total.total_tokens == 18
