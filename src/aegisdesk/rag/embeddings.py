"""Embeddings: text → a fixed-length vector, so that similar meaning ≈ nearby vectors.

Two implementations behind one small interface:

* `OllamaEmbedder` - a real embedding model (`nomic-embed-text`, 768
  dimensions) running in the local Ollama server. It captures meaning:
  "my VPN keeps dropping" lands near "the VPN disconnects frequently" even
  though they share few words.
* `HashingEmbedder` - deterministic, offline, no model at all. Each word (and
  word pair) is hashed to one of 768 positions ("the hashing trick"). It only
  captures *shared vocabulary*, not meaning, but it is instant, reproducible
  and needs no network, which is what tests and CI need. Evaluations run
  with it show a lexical baseline; Ollama embeddings should beat it.

Both return L2-normalised vectors, so cosine similarity is a dot product.
The index records which embedder built it; vectors from different models are
not comparable and must never be mixed (see `IndexInfo`).
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol

EMBEDDING_DIMENSION = 768

_STOPWORDS = frozenset(
    """a an and are as at be by can do does for from has have how i if in is it its my of on or
    our should the their them then there these this to was we what when where which who why
    will with you your yourself me am not no""".split()  # noqa: SIM905
)


@dataclass(frozen=True)
class IndexInfo:
    """Identifies the vector space an index lives in."""

    embedding_model: str
    dimension: int


class Embedder(Protocol):
    @property
    def info(self) -> IndexInfo: ...

    @property
    def default_min_score(self) -> float:
        """Similarity below which a chunk is not evidence. Scales differ per model."""
        ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def _stem(word: str) -> str:
    # Deliberately crude: enough to match "disconnects"/"disconnecting".
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _terms(text: str) -> list[str]:
    words = [_stem(w) for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())]
    words = [w for w in words if w not in _STOPWORDS and len(w) > 1]
    return words + [f"{a} {b}" for a, b in pairwise(words)]


class HashingEmbedder:
    def __init__(self, dimension: int = EMBEDDING_DIMENSION) -> None:
        self._dimension = dimension

    @property
    def info(self) -> IndexInfo:
        return IndexInfo(embedding_model="hashing-v1", dimension=self._dimension)

    @property
    def default_min_score(self) -> float:
        # Calibrated on evals/datasets/rag_v1.yaml: relevant chunks score 0.15-0.45,
        # unrelated questions stay below ~0.12.
        return 0.15

    def _embed(self, text: str) -> list[float]:
        counts: dict[str, int] = {}
        for term in _terms(text):
            counts[term] = counts.get(term, 0) + 1
        vector = [0.0] * self._dimension
        for term, count in counts.items():
            digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))  # sublinear term frequency
        return normalise(vector)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class OllamaEmbedder:
    def __init__(self, model: str, base_url: str) -> None:
        # Imported lazily so the offline path never needs the Ollama client.
        from langchain_ollama import OllamaEmbeddings

        self._model = model
        self._client = OllamaEmbeddings(model=model, base_url=base_url)

    @property
    def info(self) -> IndexInfo:
        return IndexInfo(embedding_model=f"ollama/{self._model}", dimension=EMBEDDING_DIMENSION)

    @property
    def default_min_score(self) -> float:
        # Dense models score everything higher; recalibrate with `aegisdesk eval rag`.
        return 0.5

    def _checked(self, vector: list[float]) -> list[float]:
        if len(vector) != EMBEDDING_DIMENSION:
            raise ValueError(
                f"{self._model} returned {len(vector)} dimensions; the index expects "
                f"{EMBEDDING_DIMENSION}. Use a {EMBEDDING_DIMENSION}-dimension model."
            )
        return normalise(vector)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._checked(v) for v in self._client.embed_documents(texts)]

    def embed_query(self, text: str) -> list[float]:
        return self._checked(self._client.embed_query(text))


def cosine(a: list[float], b: list[float]) -> float:
    """Dot product; equals cosine similarity because vectors are normalised."""
    return sum(x * y for x, y in zip(a, b, strict=True))
