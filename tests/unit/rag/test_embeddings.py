from __future__ import annotations

import math

from aegisdesk.rag.embeddings import EMBEDDING_DIMENSION, HashingEmbedder, cosine


def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    embedder = HashingEmbedder()
    a = embedder.embed_query("My VPN keeps disconnecting")
    b = HashingEmbedder().embed_query("My VPN keeps disconnecting")

    assert a == b
    assert len(a) == EMBEDDING_DIMENSION
    assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0)


def test_shared_vocabulary_scores_higher_than_unrelated_text() -> None:
    embedder = HashingEmbedder()
    query = embedder.embed_query("vpn disconnects every few minutes")
    related = embedder.embed_query("The VPN keeps disconnecting every 10 minutes")
    unrelated = embedder.embed_query("Invoices above EUR 10,000 need Controller approval")

    assert cosine(query, related) > cosine(query, unrelated)


def test_stopwords_only_text_has_zero_vector() -> None:
    assert not any(HashingEmbedder().embed_query("what is the of"))


def test_identifies_its_vector_space() -> None:
    info = HashingEmbedder().info
    assert (info.embedding_model, info.dimension) == ("hashing-v1", EMBEDDING_DIMENSION)
