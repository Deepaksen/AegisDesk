"""Data types that flow through the RAG pipeline.

    Document ──chunk──► Chunk ──embed──► (Chunk, vector) ──store──► ScoredChunk (at query time)

Every chunk carries its document's metadata. That is what makes citations
("DOC-VPN-001#03, v2.4") and access-control filtering possible at query time
without looking anything else up.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Classification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(pattern=r"^DOC-[A-Z]+-\d{3}$")
    title: str = Field(min_length=3)
    version: str
    effective_date: date
    department: str
    classification: Classification
    source: str
    # Who may read confidential/restricted documents (see retrieval/access.py).
    allowed_roles: tuple[str, ...] = ()
    allowed_departments: tuple[str, ...] = ()


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)

    metadata: DocumentMetadata
    body: str
    # sha256 of the raw file: lets re-ingestion skip unchanged documents.
    content_hash: str


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str  # "<document_id>#<nn>", stable across re-ingestion of the same text
    document_id: str
    index: int
    section: str  # heading the chunk came from, e.g. "Error GP-512"
    text: str
    metadata: DocumentMetadata


class ScoredChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    # Cosine similarity in [-1, 1]; higher is more similar.
    score: float
