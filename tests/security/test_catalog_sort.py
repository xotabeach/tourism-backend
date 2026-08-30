"""Sort options on the routes and places catalogue endpoints.

Both endpoints previously only offered an implicit name-ascending order
(routes also had popular/recent). The app's routes-and-places list screen
needed an explicit sort control — by name and by date added, either
direction — so these values were added to both endpoints identically.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

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
@pytest.mark.parametrize("path", ["/api/v1/routes", "/api/v1/places"])
@pytest.mark.parametrize(
    ("asc_sort", "desc_sort"),
    [("name_asc", "name_desc"), ("date_oldest", "date_newest")],
)
async def test_asc_and_desc_sorts_are_exact_reverses(
    live_app: object,
    path: str,
    asc_sort: str,
    desc_sort: str,
) -> None:
    """Cross-check both directions against each other, not a hardcoded order.

    Postgres's default collation orders Cyrillic case-insensitively (e.g.
    "галерея" before "Кара-Даг"), which disagrees with Python's codepoint
    `sorted()`. Asserting name_asc/name_desc are exact reverses of one
    another proves the DB is actually sorting by name, whatever collation
    it uses — without the test hardcoding an order of its own.
    """
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        asc = await client.get(path, params={"sort": asc_sort, "limit": 100})
        desc = await client.get(path, params={"sort": desc_sort, "limit": 100})
    assert asc.status_code == 200, asc.text
    assert desc.status_code == 200, desc.text
    asc_ids = [item["id"] for item in asc.json()["items"]]
    desc_ids = [item["id"] for item in desc.json()["items"]]
    assert len(asc_ids) >= 2, "seed data must have enough rows to prove an order"
    assert asc_ids == list(reversed(desc_ids))


@pytest.mark.asyncio
async def test_unknown_sort_value_is_rejected(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        routes = await client.get("/api/v1/routes", params={"sort": "not_a_real_sort"})
        places = await client.get("/api/v1/places", params={"sort": "not_a_real_sort"})
        assert routes.status_code == 422
        assert places.status_code == 422
