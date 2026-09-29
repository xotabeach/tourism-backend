"""M-2: unpublished place/route media must not surface as covers."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.modules.media.application.service import list_reusable_covers
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.application.place_covers import (
    covers_for_places,
    generic_fallback_cover,
)
from tourism_backend.modules.places.infrastructure.models import Place, PlaceImage

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


async def _deps_available() -> bool:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    if not await _deps_available():
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres/Redis required for integration tests are unavailable")
        pytest.skip("Postgres/Redis for integration tests are unavailable")
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_covers_for_places_skip_unpublished_place_media(session: AsyncSession) -> None:
    row = (
        await session.execute(
            select(Place.id, MediaAttachment.public_path)
            .join(
                MediaAttachment,
                MediaAttachment.entity_id == Place.id,
            )
            .where(
                Place.publication_status != "published",
                MediaAttachment.entity_type == "place",
                MediaAttachment.role == "cover",
                MediaAttachment.status == "active",
                MediaAttachment.public_path.is_not(None),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        pytest.skip("no unpublished place with an active cover attachment")
    place_id, leaked_path = row
    covers = await covers_for_places(session, [place_id])
    assert place_id not in covers

    reusable = await list_reusable_covers(session, limit=40)
    assert leaked_path not in {item.public_path for item in reusable}


@pytest.mark.asyncio
async def test_generic_fallback_cover_belongs_to_published_place(
    session: AsyncSession,
) -> None:
    url = await generic_fallback_cover(session)
    if url is None:
        pytest.skip("no published place photos in local DB")
    published_attachment = await session.scalar(
        select(MediaAttachment.id).where(
            MediaAttachment.public_path == url,
            MediaAttachment.entity_type == "place",
            MediaAttachment.entity_id.in_(
                select(Place.id).where(Place.publication_status == "published")
            ),
        )
    )
    published_image = await session.scalar(
        select(PlaceImage.id)
        .join(Place, Place.id == PlaceImage.place_id)
        .where(
            PlaceImage.source_url == url,
            Place.publication_status == "published",
        )
    )
    assert published_attachment is not None or published_image is not None


@pytest.mark.asyncio
async def test_route_cover_uses_the_chosen_stop_photo(session: AsyncSession) -> None:
    """Spec 16a, D33: the editors' pick beats the first stop's cover, and a
    removed pick falls back to it."""
    from uuid import uuid4

    from geoalchemy2 import WKTElement

    from tourism_backend.modules.geography.infrastructure.models import Region
    from tourism_backend.modules.routes.application.service import _cover_urls_for_routes
    from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

    region_id = await session.scalar(select(Region.id).where(Region.slug == "crimea"))
    if region_id is None:
        pytest.skip("no Crimea region in local DB")
    route = Route(
        id=uuid4(),
        region_id=region_id,
        name="Cover pick",
        slug=f"cover-pick-{uuid4()}",
        source="editorial",
        visibility="public",
        lifecycle_status="active",
        freshness_status="fresh",
    )
    session.add(route)
    urls: list[str] = []
    images: list[PlaceImage] = []
    for position in (1, 2):
        place = Place(
            id=uuid4(),
            region_id=region_id,
            name=f"Cover pick stop {position}",
            slug=f"cover-pick-stop-{uuid4()}",
            location=WKTElement("POINT(34.0 44.0)", srid=4326),
            publication_status="published",
            freshness_status="fresh",
        )
        session.add(place)
        await session.flush()
        url = f"https://example.test/cover-pick-{position}-{uuid4()}.jpg"
        image = PlaceImage(
            id=uuid4(),
            place_id=place.id,
            kind="photo",
            source_url=url,
            is_cover=True,
            status="active",
        )
        session.add(image)
        session.add(RouteStop(route_id=route.id, place_id=place.id, position=position))
        urls.append(url)
        images.append(image)
    await session.flush()
    try:
        assert (await _cover_urls_for_routes(session, [route.id]))[route.id] == urls[0]
        route.cover_place_image_id = images[1].id
        await session.flush()
        assert (await _cover_urls_for_routes(session, [route.id]))[route.id] == urls[1]
        images[1].status = "archived"
        await session.flush()
        assert (await _cover_urls_for_routes(session, [route.id]))[route.id] == urls[0]
    finally:
        await session.rollback()
