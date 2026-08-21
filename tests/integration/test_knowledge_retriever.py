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
        vec = retriever._vec_for("евпатория пляж лето")
        vec_lit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        async with engine.begin() as tx:  # type: ignore[attr-defined]
            await tx.execute(
                text(
                    "UPDATE knowledge_chunks SET embedding = CAST(:vec AS vector) "
                    "WHERE doc_id = :doc"
                ),
                {"vec": vec_lit, "doc": "place:evpatoria"},
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
