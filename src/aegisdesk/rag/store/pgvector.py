"""PostgreSQL + pgvector store.

Vectors live in a `vector(768)` column; `embedding <=> :query` is pgvector's
cosine *distance* (1 - cosine similarity), served by an HNSW index. The
access rule from `retrieval/access.py` is expressed in the WHERE clause, so
PostgreSQL filters before it ranks, exactly like the in-memory store.

The schema is created by Alembic (`migrations/`), never by this class.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import Text

from aegisdesk.rag.embeddings import IndexInfo
from aegisdesk.rag.models import Chunk, Classification, DocumentMetadata, ScoredChunk
from aegisdesk.rag.retrieval.access import AccessFilter
from aegisdesk.rag.store.base import check_info

# The same rule as AccessFilter.allows, in SQL.
_ACCESS_SQL = """
(
    d.classification IN ('public', 'internal')
    OR (d.classification = 'confidential'
        AND (d.allowed_roles && :roles OR :department = ANY(d.allowed_departments)))
    OR (d.classification = 'restricted' AND d.allowed_roles && :roles)
)
"""

_DOC_COLUMNS = (
    "d.document_id, d.title, d.version, d.effective_date, d.department, d.classification, "
    "d.source, d.allowed_roles, d.allowed_departments"
)


def _access_params(access: AccessFilter) -> dict[str, Any]:
    return {"roles": sorted(access.roles), "department": access.department}


def _metadata(row: Any) -> DocumentMetadata:
    return DocumentMetadata(
        document_id=row.document_id,
        title=row.title,
        version=row.version,
        effective_date=row.effective_date,
        department=row.department,
        classification=Classification(row.classification),
        source=row.source,
        allowed_roles=tuple(row.allowed_roles),
        allowed_departments=tuple(row.allowed_departments),
    )


def _chunk(row: Any) -> Chunk:
    return Chunk(
        chunk_id=row.chunk_id,
        document_id=row.document_id,
        index=row.idx,
        section=row.section,
        text=row.text,
        metadata=_metadata(row),
    )


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"


class PgVectorStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, database_url: str) -> PgVectorStore:
        return cls(create_engine(database_url, pool_pre_ping=True))

    def index_info(self) -> IndexInfo | None:
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT embedding_model, dimension FROM rag_index")).first()
        return IndexInfo(row.embedding_model, row.dimension) if row else None

    def document_hash(self, document_id: str) -> str | None:
        with self._engine.connect() as conn:
            value = conn.execute(
                text("SELECT content_hash FROM documents WHERE document_id = :id"),
                {"id": document_id},
            ).scalar()
        return str(value) if value is not None else None

    def upsert_document(
        self,
        metadata: DocumentMetadata,
        content_hash: str,
        chunks: list[Chunk],
        vectors: list[list[float]],
        info: IndexInfo,
    ) -> None:
        check_info(self.index_info(), info)
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")

        with self._engine.begin() as conn:  # one transaction: all chunks or none
            conn.execute(
                text(
                    "INSERT INTO rag_index (id, embedding_model, dimension) "
                    "VALUES (1, :model, :dim) ON CONFLICT (id) DO NOTHING"
                ),
                {"model": info.embedding_model, "dim": info.dimension},
            )
            conn.execute(
                text("DELETE FROM documents WHERE document_id = :id"),
                {"id": metadata.document_id},
            )  # cascades to its chunks
            conn.execute(
                text(
                    "INSERT INTO documents (document_id, title, version, effective_date, "
                    "department, classification, source, allowed_roles, allowed_departments, "
                    "content_hash, ingested_at) VALUES (:document_id, :title, :version, "
                    ":effective_date, :department, :classification, :source, :allowed_roles, "
                    ":allowed_departments, :content_hash, :ingested_at)"
                ).bindparams(
                    bindparam("allowed_roles", type_=ARRAY(Text)),
                    bindparam("allowed_departments", type_=ARRAY(Text)),
                ),
                {
                    **metadata.model_dump(),
                    "classification": metadata.classification.value,
                    "allowed_roles": list(metadata.allowed_roles),
                    "allowed_departments": list(metadata.allowed_departments),
                    "content_hash": content_hash,
                    "ingested_at": datetime.now(UTC),
                },
            )
            if chunks:
                conn.execute(
                    text(
                        "INSERT INTO document_chunks (chunk_id, document_id, idx, section, text, "
                        "embedding) VALUES (:chunk_id, :document_id, :idx, :section, :text, "
                        "CAST(:embedding AS vector))"
                    ),
                    [
                        {
                            "chunk_id": c.chunk_id,
                            "document_id": c.document_id,
                            "idx": c.index,
                            "section": c.section,
                            "text": c.text,
                            "embedding": _vector_literal(v),
                        }
                        for c, v in zip(chunks, vectors, strict=True)
                    ],
                )

    def reset(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM documents"))  # cascades to chunks
            conn.execute(text("DELETE FROM rag_index"))

    def delete_document(self, document_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM documents WHERE document_id = :id"), {"id": document_id})

    def search(
        self, vector: list[float], k: int, access: AccessFilter, info: IndexInfo
    ) -> list[ScoredChunk]:
        check_info(self.index_info(), info)
        query = text(
            f"SELECT c.chunk_id, c.idx, c.section, c.text, {_DOC_COLUMNS}, "  # noqa: S608
            "1 - (c.embedding <=> CAST(:query AS vector)) AS score "
            "FROM document_chunks c JOIN documents d USING (document_id) "
            f"WHERE {_ACCESS_SQL} "
            "ORDER BY c.embedding <=> CAST(:query AS vector), c.chunk_id LIMIT :k"
        ).bindparams(bindparam("roles", type_=ARRAY(Text)))
        with self._engine.connect() as conn:
            rows = conn.execute(
                query, {"query": _vector_literal(vector), "k": k, **_access_params(access)}
            ).all()
        return [ScoredChunk(chunk=_chunk(r), score=float(r.score)) for r in rows]

    def document_chunks(self, document_id: str, access: AccessFilter) -> list[Chunk]:
        query = text(
            f"SELECT c.chunk_id, c.idx, c.section, c.text, {_DOC_COLUMNS} "  # noqa: S608
            "FROM document_chunks c JOIN documents d USING (document_id) "
            f"WHERE d.document_id = :id AND {_ACCESS_SQL} ORDER BY c.idx"
        ).bindparams(bindparam("roles", type_=ARRAY(Text)))
        with self._engine.connect() as conn:
            rows = conn.execute(query, {"id": document_id, **_access_params(access)}).all()
        return [_chunk(r) for r in rows]

    def document_ids(self) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT document_id FROM documents ORDER BY 1")).all()
        return [r.document_id for r in rows]

    def chunk_count(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(text("SELECT count(*) FROM document_chunks")).scalar() or 0)
