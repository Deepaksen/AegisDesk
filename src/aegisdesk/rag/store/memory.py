"""In-memory vector store: brute-force cosine similarity in plain Python.

A few hundred chunks need no index: scoring every allowed chunk takes well
under a millisecond. This store exists so the whole RAG pipeline runs (and is
tested) with no database. The pgvector store must behave identically.
"""

from __future__ import annotations

from aegisdesk.rag.embeddings import IndexInfo, cosine
from aegisdesk.rag.models import Chunk, DocumentMetadata, ScoredChunk
from aegisdesk.rag.retrieval.access import AccessFilter
from aegisdesk.rag.store.base import check_info


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._info: IndexInfo | None = None
        self._metadata: dict[str, DocumentMetadata] = {}
        self._hashes: dict[str, str] = {}
        self._chunks: dict[str, list[tuple[Chunk, list[float]]]] = {}

    def index_info(self) -> IndexInfo | None:
        return self._info

    def document_hash(self, document_id: str) -> str | None:
        return self._hashes.get(document_id)

    def upsert_document(
        self,
        metadata: DocumentMetadata,
        content_hash: str,
        chunks: list[Chunk],
        vectors: list[list[float]],
        info: IndexInfo,
    ) -> None:
        check_info(self._info, info)
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        if any(len(v) != info.dimension for v in vectors):
            raise ValueError(f"all vectors must have {info.dimension} dimensions")
        self._info = info
        self._metadata[metadata.document_id] = metadata
        self._hashes[metadata.document_id] = content_hash
        self._chunks[metadata.document_id] = list(zip(chunks, vectors, strict=True))

    def reset(self) -> None:
        self._info = None
        self._metadata.clear()
        self._hashes.clear()
        self._chunks.clear()

    def delete_document(self, document_id: str) -> None:
        self._metadata.pop(document_id, None)
        self._hashes.pop(document_id, None)
        self._chunks.pop(document_id, None)

    def search(
        self, vector: list[float], k: int, access: AccessFilter, info: IndexInfo
    ) -> list[ScoredChunk]:
        check_info(self._info, info)
        scored = [
            ScoredChunk(chunk=chunk, score=cosine(vector, chunk_vector))
            for document_id, entries in self._chunks.items()
            if access.allows(self._metadata[document_id])  # filter BEFORE ranking
            for chunk, chunk_vector in entries
        ]
        # Ties broken by chunk ID so results are fully deterministic.
        scored.sort(key=lambda s: (-s.score, s.chunk.chunk_id))
        return scored[:k]

    def document_chunks(self, document_id: str, access: AccessFilter) -> list[Chunk]:
        metadata = self._metadata.get(document_id)
        if metadata is None or not access.allows(metadata):
            return []
        return [chunk for chunk, _ in self._chunks[document_id]]

    def document_ids(self) -> list[str]:
        return sorted(self._metadata)

    def chunk_count(self) -> int:
        return sum(len(entries) for entries in self._chunks.values())
