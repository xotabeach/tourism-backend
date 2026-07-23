import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app

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
        pytest.skip("Postgres/Redis for integration tests are unavailable")

    settings = Settings(
        environment="test",
        database_url=DATABASE_URL,
        database_url_sync=DATABASE_URL.replace("+asyncpg", "+psycopg"),
        redis_url=REDIS_URL,
    )
    app = create_app(settings)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    app.state.redis = create_redis_client(settings)
    try:
        yield app
    finally:
        await app.state.redis.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_editorial_routes_catalog_and_detail(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get(
            "/api/v1/routes",
            params={"region_slug": "crimea", "limit": 20},
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
