"""CI quality gate for retrieval (offline, hashing embedder).

Thresholds are learning-project targets calibrated in Milestone 3; see
docs/milestones/M3-rag.md for the two known lexical-embedder misses.
"""

from __future__ import annotations

from aegisdesk.config import PROJECT_ROOT
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.evals.retrieval import RagDataset, evaluate_retrieval
from aegisdesk.rag.retrieval.retriever import Retriever


def test_retrieval_quality_gate(retriever: Retriever, repository: ServiceDeskRepository) -> None:
    dataset = RagDataset.load(PROJECT_ROOT / "evals" / "datasets" / "rag_v1.yaml")

    report = evaluate_retrieval(dataset, retriever, repository)

    assert len(report.results) == 24
    assert report.access_violations == 0  # hard gate
    assert report.no_evidence_accuracy == 1.0
    assert report.hit_rate >= 0.85
    assert report.mrr >= 0.85


def test_dataset_is_valid_against_the_corpus(retriever: Retriever) -> None:
    dataset = RagDataset.load(PROJECT_ROOT / "evals" / "datasets" / "rag_v1.yaml")
    known = set(retriever.store.document_ids())
    for case in dataset.cases:
        assert set(case.expected_document_ids + case.forbidden_document_ids) <= known, case.id
        assert case.expected_document_ids or not case.answerable, case.id
