# ADR 0006: Ollama embeddings for real use, a hashing embedder for tests

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Anthropic, the primary chat-model provider, does not offer an embeddings API. The project already supports local Ollama. Tests and CI must be deterministic and offline (spec §37).

## Decision
1. Define an `Embedder` protocol (`embed_documents`, `embed_query`, `info`, `default_min_score`).
2. **Real use:** `OllamaEmbedder` with `nomic-embed-text` (768 dimensions), via `langchain_ollama.OllamaEmbeddings`.
3. **Tests, CI and the offline demo:** `HashingEmbedder`, a deterministic feature-hashing embedder (words plus word pairs hashed into 768 buckets, sublinear term frequency, L2-normalised).
4. Both use 768 dimensions, so the same schema serves both. The index records which model built it (`hashing-v1` or `ollama/nomic-embed-text`) and refuses queries from another model.
5. The evidence threshold is a property of the embedder, because similarity scales differ between models. `RAG_MIN_SCORE` overrides it, and it is calibrated with `aegisdesk eval rag`.

## Consequences
* CI runs retrieval and the RAG quality gate with no network access and fully reproducible scores.
* The hashing embedder is a lexical baseline, not a semantic model. Measured on `rag_v1`, it misses two paraphrase questions (hit rate 0.89) and cannot separate them from an unrelated question by score. The evaluation shows this limitation openly and gives the semantic embedder a baseline to beat.
* Switching embedders requires re-ingestion (`aegisdesk rag ingest --rebuild`) and recalibrating the threshold.

## Alternatives considered
* **sentence-transformers in-process:** good quality with no server, but it adds PyTorch (1–2 GB) to every install, including CI.
* **Hosted embeddings (Voyage AI, OpenAI):** high quality, but they need another API key and network access, and send document text to a third party.
* **Mocking the embeddings in tests:** it would not exercise real ranking behaviour; the hashing embedder does, deterministically.
