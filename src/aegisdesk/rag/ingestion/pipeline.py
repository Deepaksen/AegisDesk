"""Ingestion: load → clean → chunk → embed → store.

Re-running ingestion is safe and cheap: a document whose file hash has not
changed is skipped; a changed document has all its chunks replaced in one
step; a document whose file was removed is deleted from the index.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from aegisdesk.rag.embeddings import Embedder
from aegisdesk.rag.ingestion.chunker import chunk_document
from aegisdesk.rag.ingestion.loader import load_directory
from aegisdesk.rag.store.base import VectorStore


@dataclass
class IngestionReport:
    embedding_model: str
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    chunks_written: int = 0
    seconds: float = 0.0


def ingest_directory(
    directory: Path, embedder: Embedder, store: VectorStore, *, max_words: int = 160
) -> IngestionReport:
    started = time.perf_counter()
    info = embedder.info
    report = IngestionReport(embedding_model=info.embedding_model)
    documents = load_directory(directory)

    for document in documents:
        document_id = document.metadata.document_id
        previous_hash = store.document_hash(document_id)
        if previous_hash == document.content_hash:
            report.unchanged.append(document_id)
            continue

        chunks = chunk_document(document, max_words=max_words)
        vectors = embedder.embed_documents([c.text for c in chunks])
        store.upsert_document(document.metadata, document.content_hash, chunks, vectors, info)
        report.chunks_written += len(chunks)
        (report.added if previous_hash is None else report.updated).append(document_id)

    present = {d.metadata.document_id for d in documents}
    for document_id in store.document_ids():
        if document_id not in present:
            store.delete_document(document_id)
            report.removed.append(document_id)

    report.seconds = round(time.perf_counter() - started, 3)
    return report
