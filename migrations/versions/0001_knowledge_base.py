"""Knowledge base: documents and embedded chunks (pgvector).

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE rag_index (
            id              integer PRIMARY KEY CHECK (id = 1),
            embedding_model text    NOT NULL,
            dimension       integer NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE documents (
            document_id         text PRIMARY KEY,
            title               text NOT NULL,
            version             text NOT NULL,
            effective_date      date NOT NULL,
            department          text NOT NULL,
            classification      text NOT NULL
                CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
            source              text NOT NULL,
            allowed_roles       text[] NOT NULL DEFAULT '{}',
            allowed_departments text[] NOT NULL DEFAULT '{}',
            content_hash        text NOT NULL,
            ingested_at         timestamptz NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE document_chunks (
            chunk_id    text PRIMARY KEY,
            document_id text NOT NULL REFERENCES documents (document_id) ON DELETE CASCADE,
            idx         integer NOT NULL,
            section     text NOT NULL,
            text        text NOT NULL,
            embedding   vector(768) NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX document_chunks_document_idx ON document_chunks (document_id)")
    # Approximate nearest-neighbour index for cosine distance (<=>).
    op.execute(
        "CREATE INDEX document_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE document_chunks")
    op.execute("DROP TABLE documents")
    op.execute("DROP TABLE rag_index")
