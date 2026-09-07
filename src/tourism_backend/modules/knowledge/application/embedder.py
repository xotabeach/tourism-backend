"""Embedding helpers for knowledge_chunks (pgvector).

Phase 8B shipped a deterministic hashed embedder so ingest + retrieve stay
aligned without a remote model — still the default and the only one used in
tests/CI. Phase 3 (see
``tourism-platform/docs/articles-mobile-rating-context-rag-backlog-2026-09-01.md``
§4.1) adds a real ``EmbeddingProvider``: a local ``sentence-transformers``
model running in-process on the backend server, not a remote call — the doc
explicitly rejects both a hosted API (quota/latency/network in the hot path)
and LM Studio (not deployed on the production host). The chosen model,
``paraphrase-multilingual-MiniLM-L12-v2``, is 384-d — matches
``knowledge_chunks.embedding`` (migration 0032) exactly, so switching this on
needs a re-embed of existing chunks, never a schema migration.

The ``sentence-transformers`` package is an optional extra (`pip install
tourism-backend[rag]` / ``uv sync --extra rag``) — importing this module must
never require it; only actually building a non-hash embedder does.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from functools import lru_cache
from typing import Protocol

from tourism_backend.config import Settings

EMBEDDING_DIM = 384
HASH_EMBED_MODEL = "hash-v1"


class EmbeddingProvider(Protocol):
    model_id: str

    async def embed(self, text: str) -> list[float]:
        """Return an L2-normalized vector of length EMBEDDING_DIM."""

    async def warm(self) -> None:
        """Do the expensive one-time setup now, off the request path."""


class HashEmbeddingProvider:
    """Stable bag-of-tokens hash embedder (smoke / bootstrap only)."""

    def __init__(self, *, dimension: int = EMBEDDING_DIM) -> None:
        self._dimension = dimension
        self.model_id = HASH_EMBED_MODEL

    async def warm(self) -> None:
        """Nothing to load — kept so callers need not care which one they hold."""

    async def embed(self, text: str) -> list[float]:
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


class EmbeddingProviderError(RuntimeError):
    """Raised when an embedder is misconfigured or returns something we
    can't use. Callers (retriever, ingest) already fall back to FTS / skip
    the row on any exception, so this is deliberately a plain RuntimeError
    subclass rather than a new hierarchy to thread through.
    """


@lru_cache(maxsize=4)
def _load_sentence_transformer(model_name: str) -> object:
    """Load and cache a SentenceTransformer for the process lifetime.

    Loading is seconds-scale (torch + weights from disk); doing it once per
    chat turn instead of once per process would make every RAG-enabled turn
    pay that cost. ``lru_cache`` keyed by model name gives us that for free
    and naturally supports swapping models without a restart in tests.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EmbeddingProviderError(
            "sentence-transformers is not installed — install the 'rag' extra "
            "(uv sync --extra rag) to use a non-hash RAG_EMBEDDING_MODEL"
        ) from exc
    return SentenceTransformer(model_name)


# Loading is ~9.5s of CPU-and-disk work (measured on the production host),
# and it used to happen inline on the event loop inside the first `embed()`
# — freezing every other request for that whole time. The lock makes two
# chat sessions opening at once share one load instead of racing into two.
_LOAD_LOCK = asyncio.Lock()


async def _load_off_loop(model_name: str) -> object:
    async with _LOAD_LOCK:
        return await asyncio.to_thread(_load_sentence_transformer, model_name)


class SentenceTransformerEmbeddingProvider:
    """Local, in-process embedder — no network round-trip, no API quota.

    ``encode()`` is synchronous CPU work; it runs in a thread so it never
    blocks the event loop other chat turns are waiting on.
    """

    def __init__(self, *, model_name: str, dimension: int = EMBEDDING_DIM) -> None:
        self.model_id = model_name
        self._dimension = dimension

    async def warm(self) -> None:
        await _load_off_loop(self.model_id)

    async def embed(self, text: str) -> list[float]:
        model = await _load_off_loop(self.model_id)
        vector = await asyncio.to_thread(
            model.encode,  # type: ignore[attr-defined]
            text,
            normalize_embeddings=True,
        )
        floats = [float(v) for v in vector]
        if len(floats) != self._dimension:
            raise EmbeddingProviderError(
                f"Model '{self.model_id}' produced {len(floats)}-d embeddings, expected "
                f"{self._dimension} — knowledge_chunks.embedding is a fixed "
                f"vector({self._dimension}) column (migration 0032); pick a model with "
                "matching output size or add a migration to widen the column."
            )
        return floats


def default_embedder() -> HashEmbeddingProvider:
    return HashEmbeddingProvider()


def build_embedder(settings: Settings) -> EmbeddingProvider:
    """Real local embedder when configured, hash embedder otherwise.

    ``RAG_EMBEDDING_MODEL`` left at its default (``hash-v1``) keeps today's
    behaviour; any other value is treated as a ``sentence-transformers``
    model name/path and loaded locally (see module docstring).
    """
    if settings.rag_embedding_model == HASH_EMBED_MODEL:
        return default_embedder()
    return SentenceTransformerEmbeddingProvider(model_name=settings.rag_embedding_model)
