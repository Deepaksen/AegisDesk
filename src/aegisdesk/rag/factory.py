"""Build the embedder, vector store and retriever from settings."""

from __future__ import annotations

from aegisdesk.config import EmbeddingProvider, Settings, VectorStoreKind
from aegisdesk.rag.embeddings import Embedder, HashingEmbedder, OllamaEmbedder
from aegisdesk.rag.ingestion.pipeline import ingest_directory
from aegisdesk.rag.retrieval.retriever import Retriever
from aegisdesk.rag.store.base import VectorStore
from aegisdesk.rag.store.memory import InMemoryVectorStore
from aegisdesk.rag.store.pgvector import PgVectorStore


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider is EmbeddingProvider.OLLAMA:
        return OllamaEmbedder(settings.embedding_model, settings.ollama_base_url)
    return HashingEmbedder()


def build_store(settings: Settings) -> VectorStore:
    if settings.vector_store is VectorStoreKind.PGVECTOR:
        return PgVectorStore.from_url(settings.database_url)
    return InMemoryVectorStore()


def build_retriever(settings: Settings) -> Retriever:
    """A ready-to-query retriever.

    The in-memory store starts empty in every process, so it is filled from
    `documents_dir` here (fast with the hashing embedder). A pgvector store is
    persistent and must be filled with `aegisdesk rag ingest` beforehand.
    """
    embedder = build_embedder(settings)
    store = build_store(settings)
    if settings.vector_store is VectorStoreKind.MEMORY:
        ingest_directory(settings.documents_dir, embedder, store)
    min_score = (
        settings.rag_min_score if settings.rag_min_score is not None else embedder.default_min_score
    )
    return Retriever(embedder, store, top_k=settings.rag_top_k, min_score=min_score)
