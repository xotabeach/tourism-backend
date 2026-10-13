"""Weather along a route through the API (BACKEND-40)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.routes.infrastructure.models import Route
from tourism_backend.modules.weather.application.models import DayForecast

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


class _Provider:
    name = "fake"
    attribution = "Погода: тест"

    def __init__(self) -> None:
        self.down = False

    async def today(self, lat: float, lng: float) -> DayForecast:
        if self.down:
            raise TimeoutError
        return DayForecast(
            temp_min_c=20,
            temp_max_c=34,
            precipitation_probability=40,
            wind_max_ms=5,
            gust_max_ms=8,
            weather_code=95,
        )


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
async def client() -> AsyncIterator[tuple[AsyncClient, _Provider]]:
    if not await _deps_available():
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres/Redis required for integration tests are unavailable")
        pytest.skip("Postgres/Redis for integration tests are unavailable")
    app = create_app(
        Settings(
            app_env="test",
            database_url=DATABASE_URL,
            database_url_sync=DATABASE_URL.replace("+asyncpg", "+psycopg"),
            redis_url=REDIS_URL,
        )
    )
    provider = _Provider()
    app.state.weather_provider = provider
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        try:
            # Each run starts from a cold cache.
            for key in await app.state.redis.keys("weather:v1:*"):
                await app.state.redis.delete(key)
            yield http, provider
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()


async def _route(public: bool) -> str | None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as conn:
        row = await conn.scalar(
            select(Route.id)
            .where(Route.visibility == "public" if public else Route.visibility != "public")
            .limit(1)
        )
    await engine.dispose()
    return str(row) if row is not None else None


@pytest.mark.asyncio
async def test_a_public_route_gets_todays_weather_with_warnings(
    client: tuple[AsyncClient, _Provider],
) -> None:
    http, provider = client
    route_id = await _route(public=True)
    if route_id is None:
        pytest.skip("No public route seeded")
    response = await http.get(f"/api/v1/routes/{route_id}/weather")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["temp_max_c"] == 34
    assert {w["kind"] for w in body["warnings"]} == {"thunderstorm", "heat"}
    assert body["attribution"] == "Погода: тест"
    assert body["message"] is None


@pytest.mark.asyncio
async def test_no_forecast_is_said_out_loud(client: tuple[AsyncClient, _Provider]) -> None:
    http, provider = client
    route_id = await _route(public=True)
    if route_id is None:
        pytest.skip("No public route seeded")
    provider.down = True
    body = (await http.get(f"/api/v1/routes/{route_id}/weather")).json()
    assert body["status"] == "unavailable"
    assert body["message"] == "Нет данных о погоде, проверьте прогноз сами"
    assert body["warnings"] == []


@pytest.mark.asyncio
async def test_a_private_route_is_not_found(client: tuple[AsyncClient, _Provider]) -> None:
    http, _ = client
    route_id = await _route(public=False)
    if route_id is None:
        pytest.skip("No private route")
    assert (await http.get(f"/api/v1/routes/{route_id}/weather")).status_code == 404
