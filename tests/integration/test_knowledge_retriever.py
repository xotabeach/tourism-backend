"""Integration test for TourismKnowledgeRetriever against a live PostGIS/pgvector.

Requires Postgres on localhost:5433 with pgvector installed (migration 0032).
Skips gracefully when the DB is unavailable.
"""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import close_all_sessions

from tourism_backend.modules.knowledge.application.embedder import HashEmbeddingProvider
from tourism_backend.modules.knowledge.infrastructure.retriever import (
    RetrievalRequest,
    TourismKnowledgeRetriever,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)

_INSERT_SQL = """
INSERT INTO knowledge_chunks (
  id, doc_id, chunk_seq, source, title, region, locality, lang, content_type,
  body, content_hash, parsed_at, ttl_days, created_at, updated_at
) VALUES (
  md5(:doc)::uuid, :doc, 0, 'internal', :title, 'crimea', :locality, 'ru',
  :ctype, :body, :hash, now(), 365, now(), now()
)
ON CONFLICT (doc_id, chunk_seq) DO NOTHING
RETURNING id
"""


async def _ensure_table(conn) -> bool:
    try:
        result = await conn.execute(text("SELECT 1 FROM knowledge_chunks LIMIT 1"))
        result.fetchall()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def live_db() -> AsyncIterator[object]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            if not await _ensure_table(conn):
                if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
                    pytest.fail("knowledge_chunks table missing (run migrations)")
                pytest.skip("knowledge_chunks table missing")
    except Exception:  # noqa: BLE001
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres unavailable")
        pytest.skip("Postgres for integration tests unavailable")
    yield engine
    await engine.dispose()
    close_all_sessions()


@pytest.mark.asyncio
async def test_retriever_fts_and_vector_paths(live_db: object) -> None:
    engine = live_db  # type: ignore[assignment]
    retriever = TourismKnowledgeRetriever()
    async with engine.connect() as conn:  # type: ignore[attr-defined]
        # Seed two chunks (idempotent via ON CONFLICT no-op not needed here).
        await conn.execute(
            text(_INSERT_SQL),
            {
                "doc": "place:evpatoria",
                "title": "Евпатория",
                "locality": "Евпатория",
                "ctype": "tips",
                "body": "Летом с детьми удобно в Евпатории на пляже.",
                "hash": "h2" * 32,
            },
        )
        await conn.commit()

        # EVP: give one chunk a stored embedding to exercise the vector path.
        vec = await retriever._vec_for("евпатория пляж лето")
        vec_lit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        async with engine.begin() as tx:  # type: ignore[attr-defined]
            await tx.execute(
                text(
                    "UPDATE knowledge_chunks SET embedding = CAST(:vec AS vector), "
                    "embedding_model = :model WHERE doc_id = :doc"
                ),
                {"vec": vec_lit, "model": "hash-v1", "doc": "place:evpatoria"},
            )

        # FTS path (no embeddings on this query's candidate) still returns rows.
        async with engine.connect() as session:  # type: ignore[attr-defined]
            fts = await retriever.retrieve(
                session,
                request=RetrievalRequest(
                    query="евпатория пляж",
                    top_k=4,
                    locality="Евпатория",
                ),
            )
            assert fts.total_candidates >= 1
            assert any("Евпатория" in c.title for c in fts.chunks if c.title)

            # Vector path: same query, now the embedded chunk ranks.
            vecres = await retriever.retrieve(
                session,
                request=RetrievalRequest(
                    query="евпатория пляж лето",
                    top_k=4,
                    locality="Евпатория",
                ),
            )
            assert vecres.chunks
            assert any("Евпатория" in c.title for c in vecres.chunks if c.title)
            assert any(c.doc_id == "place:evpatoria" for c in vecres.chunks)


@pytest.mark.asyncio
async def test_retriever_ignores_chunks_embedded_by_a_different_model(
    live_db: object,
) -> None:
    """A stale embedding from a since-replaced model must not be compared
    against — cosine distance across unrelated vector spaces looks like a
    confident match and returns garbage, not "no match".

    The stored vector is a perfect hash-embedding of the query text itself
    (cosine == 1.0 if the model matched), and the body shares no tokens with
    the query, so FTS can't find it either — isolating the vector-path guard:
    a result here can only come from the (wrongly) compared cross-model
    vector, and its absence proves the guard skipped it.
    """
    engine = live_db  # type: ignore[assignment]
    query = "фотографировать закат на набережной"
    tagged_embedder = HashEmbeddingProvider()
    tagged_embedder.model_id = "other-model"

    async with engine.connect() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            text(_INSERT_SQL),
            {
                "doc": "place:cross-model-stub",
                "title": "Тестовое место",
                "locality": "Судак",
                "ctype": "tips",
                "body": "Здесь нет ничего общего со словами запроса совершенно.",
                "hash": "h3" * 32,
            },
        )
        await conn.commit()

        vec = await tagged_embedder.embed(query)
        vec_lit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        async with engine.begin() as tx:  # type: ignore[attr-defined]
            await tx.execute(
                text(
                    "UPDATE knowledge_chunks SET embedding = CAST(:vec AS vector), "
                    "embedding_model = :model WHERE doc_id = :doc"
                ),
                {"vec": vec_lit, "model": "other-model", "doc": "place:cross-model-stub"},
            )

        default_retriever = TourismKnowledgeRetriever()  # model_id == "hash-v1"
        async with engine.connect() as session:  # type: ignore[attr-defined]
            result = await default_retriever.retrieve(
                session,
                request=RetrievalRequest(query=query, top_k=4, locality="Судак"),
            )
            assert not any(c.doc_id == "place:cross-model-stub" for c in result.chunks)
