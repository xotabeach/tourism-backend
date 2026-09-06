"""Unit tests for hash embedder (no DB)."""

from tourism_backend.modules.knowledge.application.embedder import (
    EMBEDDING_DIM,
    HashEmbeddingProvider,
)


async def test_hash_embedder_dimension_and_stability() -> None:
    embedder = HashEmbeddingProvider()
    a = await embedder.embed("Ялта пляж лето")
    b = await embedder.embed("Ялта пляж лето")
    assert len(a) == EMBEDDING_DIM
    assert a == b
    # L2-normalized.
    assert abs(sum(v * v for v in a) - 1.0) < 1e-6


async def test_hash_embedder_differs_for_different_text() -> None:
    embedder = HashEmbeddingProvider()
    assert await embedder.embed("горы зима") != await embedder.embed("пляж лето")
