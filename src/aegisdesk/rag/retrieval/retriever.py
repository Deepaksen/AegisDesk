"""Query-time retrieval.

    question ──embed──► query vector ──store.search(top-k, access filter)──► scored chunks
                                                                          └─► keep score ≥ min_score

The minimum score is what lets the system say "I don't have evidence for
that" instead of passing the four least-bad chunks to the model and hoping it
notices they are irrelevant.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from aegisdesk.identity.context import UserContext
from aegisdesk.rag.embeddings import Embedder
from aegisdesk.rag.models import ScoredChunk
from aegisdesk.rag.retrieval.access import AccessFilter
from aegisdesk.rag.store.base import VectorStore


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    chunks: list[ScoredChunk]  # at or above min_score, best first
    below_threshold: list[ScoredChunk]  # returned by the store but too weak to use
    latency_ms: float
    top_k: int
    min_score: float

    @property
    def sufficient_evidence(self) -> bool:
        return bool(self.chunks)

    @property
    def document_ids(self) -> list[str]:
        """Distinct documents, in rank order."""
        seen: list[str] = []
        for scored in self.chunks:
            if scored.chunk.document_id not in seen:
                seen.append(scored.chunk.document_id)
        return seen


class Retriever:
    def __init__(
        self,
        embedder: Embedder,
        store: VectorStore,
        *,
        top_k: int = 4,
        min_score: float | None = None,
    ) -> None:
        self._embedder = embedder
        self._store = store
        self.top_k = top_k
        self.min_score = embedder.default_min_score if min_score is None else min_score

    @property
    def store(self) -> VectorStore:
        return self._store

    @property
    def embedding_model(self) -> str:
        return self._embedder.info.embedding_model

    def retrieve(
        self, query: str, user: UserContext, *, top_k: int | None = None
    ) -> RetrievalResult:
        k = top_k or self.top_k
        started = time.perf_counter()
        vector = self._embedder.embed_query(query)
        found = self._store.search(vector, k, AccessFilter.for_user(user), self._embedder.info)
        return RetrievalResult(
            query=query,
            chunks=[s for s in found if s.score >= self.min_score],
            below_threshold=[s for s in found if s.score < self.min_score],
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            top_k=k,
            min_score=self.min_score,
        )
