"""Redis cache in front of TourismKnowledgeRetriever (RAG phase 2).

Caches the *retrieval* (which chunks matched), never the LLM's final answer —
the answer stays personalized per session/constraints, but the same question
("что взять на пляж", "открыт ли Ай-Петри зимой") returns the same chunks
regardless of who asks, so repeat lookups skip the DB round-trip entirely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict

from redis.asyncio import Redis

from tourism_backend.modules.knowledge.infrastructure.retriever import (
    RetrievalRequest,
    RetrievalResult,
    RetrievedChunk,
)

_logger = logging.getLogger("tourism_backend.knowledge.faq_cache")
_KEY_PREFIX = "rag:retrieval:"
_WHITESPACE_RE = re.compile(r"\s+")


def _normalized_query(query: str) -> str:
    return _WHITESPACE_RE.sub(" ", query.strip().casefold())


def _cache_key(request: RetrievalRequest) -> str:
    parts = "|".join(
        (
            request.region,
            request.locality or "",
            request.content_type or "",
            str(request.top_k),
            f"{request.min_score:.3f}",
            _normalized_query(request.query),
        )
    )
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


class RagRetrievalCache:
    """Fail-open: any Redis hiccup falls back to a live retrieval, never an error."""

    def __init__(self, redis: Redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def get(self, request: RetrievalRequest) -> RetrievalResult | None:
        try:
            raw = await self._redis.get(_cache_key(request))
        except Exception:  # noqa: BLE001 — cache must never break retrieval
            _logger.warning("rag_faq_cache_get_failed", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return RetrievalResult(
                chunks=[RetrievedChunk(**chunk) for chunk in data["chunks"]],
                total_candidates=data["total_candidates"],
            )
        except Exception:  # noqa: BLE001 — a corrupt/stale entry must not break the turn
            _logger.warning("rag_faq_cache_decode_failed", exc_info=True)
            return None

    async def set(self, request: RetrievalRequest, result: RetrievalResult) -> None:
        try:
            payload = json.dumps(
                {
                    "chunks": [asdict(chunk) for chunk in result.chunks],
                    "total_candidates": result.total_candidates,
                }
            )
            await self._redis.set(_cache_key(request), payload, ex=self._ttl_seconds)
        except Exception:  # noqa: BLE001 — cache must never break retrieval
            _logger.warning("rag_faq_cache_set_failed", exc_info=True)
