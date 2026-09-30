"""LLM-as-judge for answer quality (spec section 28), opt-in.

Only for qualities code cannot check: completeness, clarity, semantic
correctness, groundedness, helpfulness. The judge prompt is versioned
(`prompts/judge/v1.yaml`) and the verdict is structured output, so scores are
parsed, not scraped from text.

The judge runs only when a judge model is configured (`EVAL_JUDGE_PROVIDER`
and `EVAL_JUDGE_MODEL`, which must be on the model allowlist). Without one the
report says "not run": there is no silent fallback to the offline fake model,
whose "opinion" would be meaningless.
"""

from __future__ import annotations

import statistics
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from aegisdesk.config import ModelProvider
from aegisdesk.evals.golden import Category
from aegisdesk.evals.runner import CaseRun
from aegisdesk.llm.client import LLMClient, StructuredOutputError
from aegisdesk.prompts.loader import Prompt

JUDGED = frozenset(
    {Category.KNOWLEDGE, Category.SERVICE_DESK, Category.ACCESS, Category.MULTI_INTENT}
)
CRITERIA = ("completeness", "clarity", "correctness", "groundedness", "helpfulness")
EVIDENCE_LIMIT = 4000


class JudgeVerdict(BaseModel):
    completeness: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    correctness: int = Field(ge=1, le=5)
    groundedness: int = Field(ge=1, le=5)
    helpfulness: int = Field(ge=1, le=5)
    rationale: str = Field(max_length=600)


def judge_input(run: CaseRun) -> str:
    evidence = "\n".join(f"[{s.tool_name}] {s.result}" for r in run.runs for s in r.tool_steps)[
        :EVIDENCE_LIMIT
    ]
    facts = "; ".join(run.case.expected.expected_facts) or "(none given)"
    return (
        f"<request>\n{run.case.text[:2000]}\n</request>\n\n"
        f"<answer>\n{run.answer}\n</answer>\n\n"
        f"<evidence>\n{evidence or '(no tool results)'}\n</evidence>\n\n"
        f"<reference_facts>\n{facts}\n</reference_facts>"
    )


class Judge:
    def __init__(
        self, model: BaseChatModel, prompt: Prompt, *, provider: ModelProvider, model_name: str
    ) -> None:
        self._client = LLMClient(model, provider, model_name)
        self._prompt = prompt
        self.label = f"{provider.value}/{model_name} prompt={prompt.name}@{prompt.version}"

    def grade(self, run: CaseRun) -> JudgeVerdict | None:
        try:
            return self._client.structured(self._prompt, judge_input(run), JudgeVerdict).value
        except StructuredOutputError:
            return None

    def grade_all(self, runs: list[CaseRun]) -> dict[str, Any]:
        verdicts: dict[str, JudgeVerdict] = {}
        unparsed: list[str] = []
        for run in runs:
            if run.skipped or run.error or run.case.category not in JUDGED:
                continue
            verdict = self.grade(run)
            if verdict is None:
                unparsed.append(run.case.id)
            else:
                verdicts[run.case.id] = verdict
        means = (
            {
                c: round(statistics.fmean(getattr(v, c) for v in verdicts.values()), 2)
                for c in CRITERIA
            }
            if verdicts
            else {}
        )
        return {
            "model": self.label,
            "judged": len(verdicts),
            "unparsed": unparsed,
            "means": means,
            "verdicts": {k: v.model_dump() for k, v in verdicts.items()},
        }
