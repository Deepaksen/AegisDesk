"""Token usage accounting.

Providers report usage differently (Anthropic: `usage.input_tokens`; Ollama:
`prompt_eval_count`). LangChain normalises both into
`AIMessage.usage_metadata`, and this module turns that into one small value
type we can sum, log and later export as metrics.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    @classmethod
    def from_message(cls, message: AIMessage) -> TokenUsage:
        """Read usage from a model response. Missing usage counts as zero, not an error."""
        usage = message.usage_metadata
        if usage is None:
            return cls()
        return cls(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"])
