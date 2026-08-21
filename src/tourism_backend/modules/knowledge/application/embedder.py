"""Embedding helpers for knowledge_chunks (pgvector).

Phase 8B ships a deterministic hashed embedder so ingest + retrieve stay
aligned without a remote model. Swap to a real EmbeddingProvider later
(LM Studio / sentence-transformers) without changing the DB schema (384-d).
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

EMBEDDING_DIM = 384
HASH_EMBED_MODEL = "hash-v1"


class EmbeddingProvider(Protocol):
    model_id: str

    def embed(self, text: str) -> list[float]:
        """Return an L2-normalized vector of length EMBEDDING_DIM."""


class HashEmbeddingProvider:
    """Stable bag-of-tokens hash embedder (smoke / bootstrap only)."""

    def __init__(self, *, dimension: int = EMBEDDING_DIM) -> None:
        self._dimension = dimension
        self.model_id = HASH_EMBED_MODEL

    def embed(self, text: str) -> list[float]:
        out = [0.0] * self._dimension
        tokens = text.casefold().split()
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:4], "little") % self._dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            out[slot] += sign
        norm = math.sqrt(sum(v * v for v in out))
        if norm > 0:
            out = [v / norm for v in out]
        return out


def default_embedder() -> HashEmbeddingProvider:
    return HashEmbeddingProvider()
