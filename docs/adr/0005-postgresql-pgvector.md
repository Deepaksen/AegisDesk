# ADR 0005: PostgreSQL + pgvector for the vector store, behind an interface

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
RAG needs similarity search over embedded chunks, filtered by access-control metadata (spec §16). The platform also needs a relational database for employees, tickets, approvals, audit events and LangGraph checkpoints (§19). Tests and CI must run without infrastructure.

## Decision
1. Store vectors in **PostgreSQL with the pgvector extension**: a `vector(768)` column, cosine distance `<=>`, and an HNSW index.
2. Express the access rule **in SQL** (`WHERE` on classification, allowed roles and allowed departments), so filtering happens before ranking, as it does in the in-memory store.
3. Manage the schema with **Alembic** migrations only. Nothing is created at application startup.
4. Hide both implementations behind a small `VectorStore` protocol, with an **in-memory store** for tests, CI and the offline demo. A shared contract test suite runs against both stores and checks that their rankings are identical.
5. Record the embedding model and dimension in the index (`rag_index`) and refuse to mix vector spaces.

## Consequences
* One database to operate, back up and secure. Documents, their access metadata and later the audit trail can be joined in SQL.
* Access filtering is enforced by the database query and tested against the real engine: in CI through a `pgvector/pgvector:pg16` service, and locally.
* HNSW is approximate. Combined with a restrictive `WHERE`, it can return fewer than k rows on large corpora; tune `hnsw.ef_search` or use iterative scans when the corpus grows.
* Very large corpora (tens of millions of vectors) or heavy vector write loads may eventually justify a dedicated vector database.

## Alternatives considered
* **Dedicated vector database (Qdrant, Weaviate, Pinecone):** strong vector features, but a second datastore with its own access-control model, and metadata duplicated across systems.
* **FAISS / in-process index only:** fast, but no persistence, transactions or SQL filtering; it would still need a database for metadata.
* **Filtering results in Python after the vector query:** simpler SQL, but it weakens access control (restricted chunks consume top-k slots) and leaks ranking information.
