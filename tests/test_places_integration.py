import os
from collections.abc import AsyncIterator

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
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
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
async def test_geography_and_places_catalog(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        countries = await client.get("/api/v1/geography/countries")
        assert countries.status_code == 200
        assert any(item["code"] == "RU" for item in countries.json())

        regions = await client.get("/api/v1/geography/regions", params={"country_code": "RU"})
        assert regions.status_code == 200
        assert any(item["slug"] == "crimea" for item in regions.json())

        localities = await client.get(
            "/api/v1/geography/localities",
            params={"region_slug": "crimea"},
        )
        assert localities.status_code == 200
        assert len(localities.json()) >= 5

        places = await client.get(
            "/api/v1/places",
            params={"region_slug": "crimea", "limit": 50},
        )
        assert places.status_code == 200
        body = places.json()
        assert body["total"] >= 20
        assert len(body["items"]) >= 20

        place_id = body["items"][0]["id"]
        detail = await client.get(f"/api/v1/places/{place_id}")
        assert detail.status_code == 200
        assert detail.json()["name"]
        assert detail.json()["categories"]

        filtered = await client.get(
            "/api/v1/places",
            params={"region_slug": "crimea", "category": "palace", "q": "дворец"},
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] >= 1
