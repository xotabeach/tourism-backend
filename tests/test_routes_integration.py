import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from geoalchemy2.elements import WKTElement
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute
from tourism_backend.modules.geography.infrastructure.models import Region
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


async def _deps_available() -> bool:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as conn:
            from sqlalchemy import text

            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def live_app() -> AsyncIterator[object]:
    if not await _deps_available():
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres/Redis required for integration tests are unavailable")
        pytest.skip("Postgres/Redis for integration tests are unavailable")

    settings = Settings(
        app_env="test",
        database_url=DATABASE_URL,
        database_url_sync=DATABASE_URL.replace("+asyncpg", "+psycopg"),
        redis_url=REDIS_URL,
    )
    app = create_app(settings)
    try:
        yield app
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


@pytest.mark.asyncio
async def test_editorial_routes_catalog_and_detail(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(
            "/api/v1/routes",
            params={"region_slug": "crimea", "source": "editorial", "limit": 20},
        )
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] >= 3
        assert all(item["source"] == "editorial" for item in body["items"])
        assert all(item["visibility"] == "public" for item in body["items"])
        assert all(item["lifecycle_status"] == "active" for item in body["items"])
        assert all(item["stops_count"] >= 2 for item in body["items"])

        route_id = body["items"][0]["id"]
        detail = await client.get(f"/api/v1/routes/{route_id}")
        assert detail.status_code == 200
        card = detail.json()
        assert len(card["stops"]) >= 2
        assert card["stops"][0]["position"] == 1
        assert card["stops"][0]["place_name"]
        assert card["stops"][0]["lat"] is not None


@pytest.mark.asyncio
async def test_routes_filters_and_unpublished_not_found(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        filtered = await client.get(
            "/api/v1/routes",
            params={"region_slug": "crimea", "transport_mode": "car", "difficulty": "easy"},
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] >= 1

        missing = await client.get(f"/api/v1/routes/{uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "route_not_found"

        oversized = await client.get("/api/v1/routes", params={"q": "x" * 201})
        assert oversized.status_code == 422

        invalid_sort = await client.get("/api/v1/routes", params={"sort": "unknown"})
        assert invalid_sort.status_code == 422


@pytest.mark.asyncio
async def test_routes_can_be_sorted_by_popularity(live_app: object) -> None:
    session_factory = live_app.state.session_factory  # type: ignore[attr-defined]
    suffix = uuid4().hex
    less_popular_id = uuid4()
    popular_id = uuid4()
    user_id = uuid4()
    query = f"Popularity {suffix}"
    async with session_factory() as session:
        region_id = await session.scalar(select(Region.id).where(Region.slug == "crimea"))
        assert region_id is not None
        session.add_all(
            [
                Route(
                    id=less_popular_id,
                    region_id=region_id,
                    name=f"{query} A",
                    slug=f"popularity-a-{suffix}",
                    source="editorial",
                    visibility="public",
                    lifecycle_status="active",
                    publication_status="published",
                ),
                Route(
                    id=popular_id,
                    region_id=region_id,
                    name=f"{query} B",
                    slug=f"popularity-b-{suffix}",
                    source="editorial",
                    visibility="public",
                    lifecycle_status="active",
                    publication_status="published",
                ),
                User(
                    id=user_id,
                    display_name="Popularity tester",
                    phone_e164=f"+79{uuid4().int % 1_000_000_000:09d}",
                ),
            ]
        )
        await session.flush()
        session.add(FavoriteRoute(user_id=user_id, route_id=popular_id))
        await session.commit()

    try:
        transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/routes",
                params={"q": query, "sort": "popular", "limit": 2},
            )
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [
            str(popular_id),
            str(less_popular_id),
        ]
    finally:
        async with session_factory() as session:
            for route_id in (less_popular_id, popular_id):
                route = await session.get(Route, route_id)
                if route is not None:
                    await session.delete(route)
            user = await session.get(User, user_id)
            if user is not None:
                await session.delete(user)
            await session.commit()


@pytest.mark.asyncio
async def test_public_route_with_unpublished_stop_is_not_exposed(live_app: object) -> None:
    session_factory = live_app.state.session_factory  # type: ignore[attr-defined]
    route_id = uuid4()
    place_id = uuid4()
    async with session_factory() as session:
        region_id = await session.scalar(select(Region.id).where(Region.slug == "crimea"))
        assert region_id is not None
        place = Place(
            id=place_id,
            region_id=region_id,
            name="Private review place",
            slug=f"private-review-{place_id}",
            location=WKTElement("POINT(34.0 44.0)", srid=4326),
            publication_status="draft",
            freshness_status="fresh",
        )
        route = Route(
            id=route_id,
            region_id=region_id,
            name="Route with private stop",
            slug=f"private-stop-route-{route_id}",
            source="editorial",
            visibility="public",
            lifecycle_status="active",
            freshness_status="fresh",
        )
        session.add_all([place, route])
        await session.flush()
        session.add(
            RouteStop(
                route_id=route_id,
                place_id=place_id,
                position=1,
            )
        )
        await session.commit()

    try:
        transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            detail = await client.get(f"/api/v1/routes/{route_id}")
            listed = await client.get("/api/v1/routes", params={"limit": 100})

        assert detail.status_code == 404
        assert all(item["id"] != str(route_id) for item in listed.json()["items"])
        assert "Private review place" not in str(listed.json())
    finally:
        async with session_factory() as session:
            await session.execute(delete(RouteStop).where(RouteStop.route_id == route_id))
            stored_route = await session.get(Route, route_id)
            stored_place = await session.get(Place, place_id)
            if stored_route is not None:
                await session.delete(stored_route)
            if stored_place is not None:
                await session.delete(stored_place)
            await session.commit()
