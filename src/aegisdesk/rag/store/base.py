"""The vector store interface shared by the in-memory and pgvector stores."""

from __future__ import annotations

from typing import Protocol

from aegisdesk.rag.embeddings import IndexInfo
from aegisdesk.rag.models import Chunk, DocumentMetadata, ScoredChunk
from aegisdesk.rag.retrieval.access import AccessFilter


class EmbeddingMismatchError(RuntimeError):
    """The index was built with a different embedding model or dimension."""


class VectorStore(Protocol):
    """Stores chunks with their vectors and searches them under an access filter."""

    def index_info(self) -> IndexInfo | None:
        """The embedding space of the stored vectors, or None if the store is empty."""
        ...

    def document_hash(self, document_id: str) -> str | None: ...

    def upsert_document(
        self,
        metadata: DocumentMetadata,
        content_hash: str,
        chunks: list[Chunk],
        vectors: list[list[float]],
        info: IndexInfo,
    ) -> None:
        """Replace all chunks of one document atomically."""
        ...

    def delete_document(self, document_id: str) -> None: ...

    def reset(self) -> None:
        """Remove everything, including the recorded embedding model."""
        ...

    def search(
        self, vector: list[float], k: int, access: AccessFilter, info: IndexInfo
    ) -> list[ScoredChunk]:
        """Top-k chunks by cosine similarity, among documents `access` allows only."""
        ...

    def document_chunks(self, document_id: str, access: AccessFilter) -> list[Chunk]:
        """All chunks of one document in order, or [] if missing or not allowed."""
        ...

    def document_ids(self) -> list[str]: ...

    def chunk_count(self) -> int: ...


def check_info(stored: IndexInfo | None, requested: IndexInfo) -> None:
    if stored is not None and stored != requested:
        raise EmbeddingMismatchError(
            f"Index was built with {stored.embedding_model} ({stored.dimension}d) but "
            f"{requested.embedding_model} ({requested.dimension}d) was requested. "
            "Re-ingest with `aegisdesk rag ingest --rebuild`."
        )
