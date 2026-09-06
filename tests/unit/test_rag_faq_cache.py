"""Unit tests for RagRetrievalCache (no real Redis)."""

from tourism_backend.modules.knowledge.infrastructure.faq_cache import RagRetrievalCache
from tourism_backend.modules.knowledge.infrastructure.retriever import (
    RetrievalRequest,
    RetrievalResult,
    RetrievedChunk,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        del ex
        self.store[key] = value


class _ExplodingRedis:
    async def get(self, key: str) -> str | None:
        raise ConnectionError("redis is down")

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        raise ConnectionError("redis is down")


def _sample_result() -> RetrievalResult:
    return RetrievalResult(
        chunks=[
            RetrievedChunk(
                chunk_id="c1",
                doc_id="place:ai-petri",
                title="Ай-Петри",
                body="Горный пик с панорамой ЮБК.",
                source="internal",
                content_type="overview",
                locality="Ялта",
                place_id="p1",
                score=0.91,
            )
        ],
        total_candidates=3,
    )


async def test_get_misses_on_empty_cache() -> None:
    cache = RagRetrievalCache(_FakeRedis(), ttl_seconds=3600)  # type: ignore[arg-type]
    result = await cache.get(RetrievalRequest(query="гора"))
    assert result is None


async def test_set_then_get_round_trips() -> None:
    redis = _FakeRedis()
    cache = RagRetrievalCache(redis, ttl_seconds=3600)  # type: ignore[arg-type]
    request = RetrievalRequest(query="гора с канатной дорогой")
    original = _sample_result()

    await cache.set(request, original)
    cached = await cache.get(request)

    assert cached is not None
    assert cached.total_candidates == original.total_candidates
    assert cached.chunks == original.chunks


async def test_query_normalization_shares_a_cache_key() -> None:
    redis = _FakeRedis()
    cache = RagRetrievalCache(redis, ttl_seconds=3600)  # type: ignore[arg-type]
    await cache.set(RetrievalRequest(query="  Гора  С Канатной   дорогой "), _sample_result())

    cached = await cache.get(RetrievalRequest(query="гора с канатной дорогой"))

    assert cached is not None


async def test_different_top_k_misses_a_differently_scoped_cache_entry() -> None:
    redis = _FakeRedis()
    cache = RagRetrievalCache(redis, ttl_seconds=3600)  # type: ignore[arg-type]
    await cache.set(RetrievalRequest(query="гора", top_k=4), _sample_result())

    cached = await cache.get(RetrievalRequest(query="гора", top_k=8))

    assert cached is None


async def test_get_fails_open_on_redis_error() -> None:
    cache = RagRetrievalCache(_ExplodingRedis(), ttl_seconds=3600)  # type: ignore[arg-type]
    result = await cache.get(RetrievalRequest(query="гора"))
    assert result is None


async def test_set_fails_open_on_redis_error() -> None:
    cache = RagRetrievalCache(_ExplodingRedis(), ttl_seconds=3600)  # type: ignore[arg-type]
    # Must not raise.
    await cache.set(RetrievalRequest(query="гора"), _sample_result())


async def test_get_returns_none_on_corrupt_cached_json() -> None:
    redis = _FakeRedis()
    request = RetrievalRequest(query="гора")
    cache = RagRetrievalCache(redis, ttl_seconds=3600)  # type: ignore[arg-type]
    # Bypass .set() to write something the decoder can't use.
    from tourism_backend.modules.knowledge.infrastructure.faq_cache import _cache_key

    redis.store[_cache_key(request)] = '{"chunks": "not-a-list"}'

    result = await cache.get(request)

    assert result is None
