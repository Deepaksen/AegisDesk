"""One behavioural contract, two vector stores.

Every test runs against the in-memory store, and against pgvector when
AEGIS_TEST_DATABASE_URL points at a migrated database (CI provides one; the
tests reset its contents, so never point it at a database you care about).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from aegisdesk.config import PROJECT_ROOT
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.evals.retrieval import RagDataset
from aegisdesk.identity.context import authenticate
from aegisdesk.rag.embeddings import HashingEmbedder, IndexInfo
from aegisdesk.rag.ingestion.chunker import chunk_document
from aegisdesk.rag.ingestion.loader import load_document
from aegisdesk.rag.ingestion.pipeline import ingest_directory
from aegisdesk.rag.retrieval.access import AccessFilter
from aegisdesk.rag.store.base import EmbeddingMismatchError, VectorStore
from aegisdesk.rag.store.memory import InMemoryVectorStore
from aegisdesk.rag.store.pgvector import PgVectorStore

DOCS = PROJECT_ROOT / "data" / "documents"
TEST_DB_URL = os.environ.get("AEGIS_TEST_DATABASE_URL")
EMBEDDER = HashingEmbedder()


@pytest.fixture(params=["memory", "pgvector"])
def store(request: pytest.FixtureRequest) -> Iterator[VectorStore]:
    if request.param == "memory":
        yield InMemoryVectorStore()
        return
    if not TEST_DB_URL:
        pytest.skip("AEGIS_TEST_DATABASE_URL not set")
    pg = PgVectorStore.from_url(TEST_DB_URL)
    pg.reset()
    yield pg
    pg.reset()


@pytest.fixture
def filled(store: VectorStore) -> VectorStore:
    ingest_directory(DOCS, EMBEDDER, store)
    return store


def _access(employee_id: str) -> AccessFilter:
    repository = ServiceDeskRepository.from_seed(PROJECT_ROOT / "data" / "seed")
    return AccessFilter.for_user(authenticate(repository, employee_id))


def _search(store: VectorStore, query: str, employee_id: str, k: int = 4) -> list[str]:
    results = store.search(EMBEDDER.embed_query(query), k, _access(employee_id), EMBEDDER.info)
    return [r.chunk.chunk_id for r in results]


def test_ingested_corpus_is_stored(filled: VectorStore) -> None:
    assert filled.chunk_count() == 48
    assert len(filled.document_ids()) == 12
    assert filled.index_info() == EMBEDDER.info


def test_search_ranks_by_similarity_and_respects_k(filled: VectorStore) -> None:
    results = filled.search(
        EMBEDDER.embed_query("error GP-512 certificate"), 3, _access("E1004"), EMBEDDER.info
    )
    assert len(results) == 3
    assert results[0].chunk.chunk_id == "DOC-VPN-001#05"
    assert [r.score for r in results] == sorted((r.score for r in results), reverse=True)


@pytest.mark.parametrize(
    ("employee_id", "visible", "hidden"),
    [
        ("E1004", {"DOC-FIN-001"}, {"DOC-PDB-001", "DOC-INC-001"}),  # finance employee
        ("E1001", set(), {"DOC-PDB-001", "DOC-INC-001", "DOC-FIN-001"}),  # engineer
        ("E1005", set(), {"DOC-PDB-001", "DOC-INC-001", "DOC-FIN-001"}),  # contractor
        ("E1006", {"DOC-PDB-001", "DOC-INC-001"}, {"DOC-FIN-001"}),  # IT admin
        ("E1010", {"DOC-INC-001", "DOC-FIN-001"}, {"DOC-PDB-001"}),  # finance manager
    ],
)
def test_access_filter_matrix(
    filled: VectorStore, employee_id: str, visible: set[str], hidden: set[str]
) -> None:
    access = _access(employee_id)
    for document_id in visible:
        assert filled.document_chunks(document_id, access), document_id
    for document_id in hidden:
        assert filled.document_chunks(document_id, access) == [], document_id
        # Even a query made of the hidden document's own words cannot reach it.
        own_words = filled.document_chunks(document_id, _access("E1013"))  # IT service manager
        text = " ".join(c.text for c in own_words) or document_id
        results = _search(filled, text[:400], employee_id, k=8)
        assert not any(r.startswith(document_id) for r in results), (document_id, results)


def test_filter_is_applied_before_ranking(filled: VectorStore) -> None:
    # The best matches are restricted, yet an ordinary employee still gets k usable results.
    results = _search(filled, "production database bastion break-glass CAB write", "E1004", k=4)
    assert len(results) == 4
    assert not any(r.startswith("DOC-PDB-001") for r in results)


def test_upsert_replaces_a_documents_chunks(filled: VectorStore) -> None:
    document = load_document(DOCS / "it-handbook.md")
    first_chunk = chunk_document(document)[:1]
    filled.upsert_document(
        document.metadata,
        "new-hash",
        first_chunk,
        EMBEDDER.embed_documents([c.text for c in first_chunk]),
        EMBEDDER.info,
    )
    assert filled.document_hash("DOC-IT-001") == "new-hash"
    assert [c.chunk_id for c in filled.document_chunks("DOC-IT-001", _access("E1004"))] == [
        "DOC-IT-001#01"
    ]
    assert filled.chunk_count() == 48 - 3


def test_vectors_from_another_embedding_model_are_refused(filled: VectorStore) -> None:
    other = IndexInfo(embedding_model="ollama/nomic-embed-text", dimension=768)
    with pytest.raises(EmbeddingMismatchError, match="Re-ingest"):
        filled.search(EMBEDDER.embed_query("vpn"), 4, _access("E1004"), other)


def test_reset_clears_everything(filled: VectorStore) -> None:
    filled.reset()
    assert filled.chunk_count() == 0
    assert filled.index_info() is None


def test_both_stores_rank_identically(filled: VectorStore) -> None:
    reference = InMemoryVectorStore()
    ingest_directory(DOCS, EMBEDDER, reference)
    dataset = RagDataset.load(Path(PROJECT_ROOT / "evals/datasets/rag_v1.yaml"))
    for case in dataset.cases:
        assert _search(filled, case.question, case.user, k=8) == _search(
            reference, case.question, case.user, k=8
        ), case.id
