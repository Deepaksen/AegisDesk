"""Deterministic retrieval evaluation.

Every metric here is computed by plain code from IDs, so no LLM judge is
needed (spec §28: don't use an LLM for what code can verify).

* hit rate@k     - share of answerable cases where at least one expected
                   document was retrieved above the evidence threshold
* recall@k       - share of expected documents retrieved, averaged over cases
* MRR            - mean of 1/rank of the first expected document (0 if missing)
* no-evidence    - share of out-of-scope cases where retrieval returned nothing
  accuracy         usable, so the system will say "I don't know"
* access         - forbidden chunks returned *at all* (even below threshold).
  violations       Must be 0; a single violation fails the gate.

Answer-level metrics (faithfulness, fact coverage, LLM-as-judge) are added in
Milestone 9; `expected_facts` is already in the dataset for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import authenticate
from aegisdesk.rag.retrieval.retriever import Retriever


class RagCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    user: str
    question: str
    expected_document_ids: list[str]
    expected_facts: list[str] = []
    forbidden_document_ids: list[str] = []
    answerable: bool
    expect_no_evidence: bool = False


class RagDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int
    cases: list[RagCase]

    @classmethod
    def load(cls, path: Path) -> RagDataset:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class CaseResult:
    case: RagCase
    retrieved_document_ids: list[str]
    scores: list[float]
    first_relevant_rank: int | None
    access_violations: list[str]
    latency_ms: float

    @property
    def hit(self) -> bool:
        return self.first_relevant_rank is not None

    @property
    def recall(self) -> float:
        expected = set(self.case.expected_document_ids)
        if not expected:
            return 1.0
        return len(expected & set(self.retrieved_document_ids)) / len(expected)

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.first_relevant_rank if self.first_relevant_rank else 0.0

    @property
    def passed(self) -> bool:
        if self.access_violations:
            return False
        if self.case.expect_no_evidence:
            return not self.retrieved_document_ids
        if self.case.answerable:
            return self.hit
        return True


@dataclass
class RagEvalReport:
    dataset: str
    dataset_version: int
    embedding_model: str
    top_k: int
    min_score: float
    results: list[CaseResult] = field(default_factory=list)

    def _answerable(self) -> list[CaseResult]:
        return [r for r in self.results if r.case.answerable]

    @property
    def hit_rate(self) -> float:
        cases = self._answerable()
        return sum(r.hit for r in cases) / len(cases) if cases else 0.0

    @property
    def recall(self) -> float:
        cases = self._answerable()
        return sum(r.recall for r in cases) / len(cases) if cases else 0.0

    @property
    def mrr(self) -> float:
        cases = self._answerable()
        return sum(r.reciprocal_rank for r in cases) / len(cases) if cases else 0.0

    @property
    def no_evidence_accuracy(self) -> float:
        cases = [r for r in self.results if r.case.expect_no_evidence]
        return sum(not r.retrieved_document_ids for r in cases) / len(cases) if cases else 1.0

    @property
    def access_violations(self) -> int:
        return sum(len(r.access_violations) for r in self.results)

    @property
    def mean_latency_ms(self) -> float:
        return sum(r.latency_ms for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]


def evaluate_retrieval(
    dataset: RagDataset, retriever: Retriever, repository: ServiceDeskRepository
) -> RagEvalReport:
    report = RagEvalReport(
        dataset=dataset.name,
        dataset_version=dataset.version,
        embedding_model=retriever.embedding_model,
        top_k=retriever.top_k,
        min_score=retriever.min_score,
    )
    for case in dataset.cases:
        result = retriever.retrieve(case.question, authenticate(repository, case.user))
        ranked = result.document_ids
        rank = next(
            (i + 1 for i, doc in enumerate(ranked) if doc in case.expected_document_ids), None
        )
        # Anything the store returned counts, including chunks below the threshold.
        returned = {s.chunk.document_id for s in result.chunks + result.below_threshold}
        report.results.append(
            CaseResult(
                case=case,
                retrieved_document_ids=ranked,
                scores=[round(s.score, 3) for s in result.chunks],
                first_relevant_rank=rank,
                access_violations=sorted(returned & set(case.forbidden_document_ids)),
                latency_ms=result.latency_ms,
            )
        )
    return report
