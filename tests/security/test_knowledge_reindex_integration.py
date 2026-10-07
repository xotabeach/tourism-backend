"""The nightly reconciliation against a real database (spec 18, D13)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from geoalchemy2 import WKTElement
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.knowledge.application.embedder import default_embedder
from tourism_backend.modules.knowledge.application.reindex_job import reconcile_once
from tourism_backend.modules.knowledge.infrastructure.models import KnowledgeChunk
from tourism_backend.modules.places.infrastructure.models import Place

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM knowledge_chunks LIMIT 1"))
    except Exception:  # noqa: BLE001
        await engine.dispose()
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres required for integration tests is unavailable")
        pytest.skip("Postgres for integration tests is unavailable")
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _chunks(factory: async_sessionmaker[AsyncSession], doc_id: str) -> list[tuple]:
    async with factory() as session:
        rows = await session.execute(
            text(
                "SELECT body, embedding_model, embedding IS NOT NULL FROM knowledge_chunks "
                "WHERE doc_id = :doc ORDER BY chunk_seq"
            ),
            {"doc": doc_id},
        )
        return [tuple(row) for row in rows.all()]


@pytest.mark.asyncio
async def test_index_follows_what_is_published(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    place_id = uuid4()
    doc_id = f"place:{place_id}"
    embedder = default_embedder()
    async with factory() as session:
        region_id = await session.scalar(select(Region.id).where(Region.slug == "crimea"))
        if region_id is None:
            pytest.skip("no Crimea region in the test database")
        session.add(
            Place(
                id=place_id,
                region_id=region_id,
                name=f"Сверка индекса {place_id.hex[:8]}",
                slug=f"reindex-{place_id}",
                short_description="Скала над морем.",
                description="Скала над морем с видом на бухту и старую тропу.",
                location=WKTElement("POINT(34.0 44.0)", srid=4326),
                publication_status="published",
                freshness_status="fresh",
            )
        )
        await session.commit()
    try:
        first = await reconcile_once(factory, embedder, pause_seconds=0)
        assert first is not None
        assert first["inserted"] >= 1
        indexed = await _chunks(factory, doc_id)
        assert indexed
        assert all(model == embedder.model_id and embedded for _b, model, embedded in indexed)
        assert any("старую тропу" in body for body, _m, _e in indexed)

        # Nothing changed: the second night writes nothing for this place.
        again = await reconcile_once(factory, embedder, pause_seconds=0)
        assert again is not None
        assert (again["inserted"], again["updated"], again["embedded"]) == (0, 0, 0)

        async with factory() as session:
            place = await session.get(Place, place_id)
            assert place is not None
            place.description = "Скала над морем с видом на бухту и маяк."
            await session.commit()
        changed = await reconcile_once(factory, embedder, pause_seconds=0)
        assert changed is not None
        assert changed["updated"] + changed["inserted"] >= 1
        bodies = " ".join(body for body, _m, _e in await _chunks(factory, doc_id))
        assert "маяк" in bodies
        assert "старую тропу" not in bodies

        async with factory() as session:
            place = await session.get(Place, place_id)
            assert place is not None
            place.publication_status = "draft"
            await session.commit()
        gone = await reconcile_once(factory, embedder, pause_seconds=0)
        assert gone is not None
        assert gone["removed"] >= 1
        assert await _chunks(factory, doc_id) == []
    finally:
        async with factory() as session:
            await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id))
            await session.execute(delete(Place).where(Place.id == place_id))
            await session.commit()
