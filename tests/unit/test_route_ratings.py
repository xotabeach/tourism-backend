"""The catalogue card's rating.

Until 2026-09-04 the card derived a score from the route's *name*
(`'4,${9 - (route.name.length % 3)}'`), so every route in the catalogue
showed 4.7, 4.8 or 4.9 and none of it meant anything. These tests pin the
real aggregate, and in particular the two cases that make it honest: a
route nobody rated has no score at all, and replies are not ratings.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.routes.application.service import route_ratings
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_route(session: AsyncSession, region_id: UUID, name: str) -> UUID:
    route_id = uuid4()
    session.add(
        Route(
            id=route_id,
            region_id=region_id,
            name=name,
            slug=f"route-{route_id.hex[:10]}",
            source="editorial",
            visibility="public",
            lifecycle_status="active",
            publication_status="published",
            freshness_status="unknown",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    return route_id


@pytest.mark.asyncio
async def test_rating_counts_only_published_root_reviews(session: AsyncSession) -> None:
    # Берём существующий регион, а не создаём: у regions есть обязательный
    # country_id, и подделывать географию ради теста про оценки незачем.
    region_id = await session.scalar(select(Region.id).limit(1))
    assert region_id is not None, "в тестовой базе нет ни одного региона"
    rated_id = await _seed_route(session, region_id, "С оценками")
    unrated_id = await _seed_route(session, region_id, "Без оценок")
    author_id = uuid4()
    session.add(
        User(
            id=author_id,
            phone_e164=f"+7955{uuid4().int % 10_000_000:07d}",
            display_name="Оценщик",
        )
    )
    await session.flush()

    def review(rating: int, status: str, reply_to: UUID | None = None) -> RouteReview:
        return RouteReview(
            id=uuid4(),
            route_id=rated_id,
            author_user_id=author_id,
            rating=rating,
            body="Отзыв",
            status=status,
            reply_to_review_id=reply_to,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    root = review(5, "published")
    session.add_all([root, review(4, "published")])
    # Neither of these may move the average.
    session.add_all([review(1, "rejected"), review(1, "pending_review")])
    await session.flush()
    # A reply carries a rating column it never meant — it is not a rating.
    session.add(review(1, "published", reply_to=root.id))
    await session.commit()

    try:
        ratings = await route_ratings(session, [rated_id, unrated_id])
        assert ratings[rated_id] == (4.5, 2)
        # Absent, not zero: an empty star reads as "bad", and that would be a
        # lie about a route nobody has rated yet.
        assert unrated_id not in ratings
    finally:
        # The rows were committed, so a rollback would not undo them — and a
        # published editorial route with no stops leaks into the catalogue
        # tests that assert every card has at least two.
        await session.execute(delete(RouteReview).where(RouteReview.route_id == rated_id))
        await session.execute(delete(Route).where(Route.id.in_([rated_id, unrated_id])))
        await session.execute(delete(User).where(User.id == author_id))
        await session.commit()


@pytest.mark.asyncio
async def test_rating_lookup_is_empty_for_no_ids(session: AsyncSession) -> None:
    assert await route_ratings(session, []) == {}
