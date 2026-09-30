# RAG design

How AegisDesk answers questions from Northstar's documentation. The learning notes with measured results are in [milestones/M3-rag.md](milestones/M3-rag.md).

## Pipeline

```
 data/documents/*.md  (YAML front matter: id, title, version, effective_date, department,
        │              classification, allowed_roles, allowed_departments, source)
        ▼
 load ─► validate metadata (reject unclassified/duplicate) ─► content hash
        ▼
 clean ─► chunk by heading, ~160 words, 1-paragraph overlap, "Title — Section" prefix
        ▼            chunk_id = DOC-XXX-NNN#NN (stable)
 embed ─► HashingEmbedder (offline) | OllamaEmbedder (nomic-embed-text), 768-d, L2-normalised
        ▼
 store ─► InMemoryVectorStore | PgVectorStore (vector(768), HNSW cosine), one transaction per document
          records embedding model + dimension; refuses mixed vector spaces
```

Re-ingestion skips unchanged documents (hash), replaces changed ones and deletes removed ones.

## Query path

```
 question + UserContext
        ▼
 embed query ─► store.search(k, AccessFilter(user)) ─► filter BEFORE ranking
        ▼
 keep score ≥ min_score (embedder-specific default; RAG_MIN_SCORE overrides)
        ├── nothing left ──► "insufficient evidence" (no model call)
        ▼
 context: <documents><document chunk_id=… title=… version=…>…</document></documents>
        ▼
 LLM structured output: {answer, citations[chunk_id], insufficient_evidence}
        ▼
 verify: citations non-empty and ⊆ retrieved chunk IDs, else discard → safe fallback
```

## Access rule (`rag/retrieval/access.py`, mirrored in SQL in `rag/store/pgvector.py`)

| Classification | Visible to |
|---|---|
| public, internal | every signed-in employee, including contractors |
| confidential | users with one of `allowed_roles`, or in one of `allowed_departments` |
| restricted | users with one of `allowed_roles` |

Corpus examples: the Incident Escalation Guide is confidential (it_admin, manager); the FinanceERP User Guide is confidential (finance department); the Production Database Access Policy is restricted (it_admin).

## Trust model

* Retrieved text is **untrusted data**. Prompts say so, tool results carry a note, and context delimiters cannot be forged from inside a chunk.
* None of that is relied on. Access filtering happens before the model sees anything, and every action still goes through `ToolExecutor` (tool allowlist, strict schemas, identity from the session).
* Citations are checked by code in the plain-RAG path.

## Interfaces

| Component | Module |
|---|---|
| `Embedder` protocol, `HashingEmbedder`, `OllamaEmbedder` | `rag/embeddings.py` |
| `VectorStore` protocol | `rag/store/base.py` |
| In-memory / pgvector stores | `rag/store/memory.py`, `rag/store/pgvector.py` |
| Ingestion | `rag/ingestion/{loader,chunker,pipeline}.py` |
| Retriever | `rag/retrieval/retriever.py` |
| Grounded answers | `rag/answer.py` (prompt `grounded_answer@v1`) |
| Agent tools | `tools/knowledge.py` (`search_knowledge_base`, `retrieve_document`) |
| Evaluation | `evals/retrieval.py` + `evals/datasets/rag_v1.yaml` |
| Schema | `migrations/versions/0001_knowledge_base.py` |

## Configuration

`EMBEDDING_PROVIDER` (hash|ollama), `EMBEDDING_MODEL`, `VECTOR_STORE` (memory|pgvector), `DATABASE_URL`, `RAG_TOP_K`, `RAG_MIN_SCORE`, `DOCUMENTS_DIR`.

## Evaluation

Deterministic metrics per dataset version: hit rate@k, recall@k, MRR, no-evidence accuracy, access violations (hard gate: 0). Answer-level metrics (faithfulness, fact coverage, LLM-as-judge) are added in M9.
