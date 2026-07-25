"""Security regression tests for currently implemented public APIs.

Auth/BOLA/CSRF/file upload tests are deferred until those features exist.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.config import Settings, validate_settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")

SQLI_PAYLOADS = [
    "' OR '1'='1",
    "1; DROP TABLE places;--",
    "'; DROP TABLE places --",
    "%'; DROP TABLE places --",
    "'; SELECT pg_sleep(5) --",
]

XSS_LIKE_PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "javascript:alert(1)",
    "<svg/onload=alert(1)>",
]


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


def test_validate_settings_rejects_local_password_in_production() -> None:
    settings = Settings(
        environment="production",
        database_url=("postgresql+asyncpg://tourism:local-tourism-password@db:5432/tourism"),
        database_url_sync=("postgresql+psycopg://tourism:local-tourism-password@db:5432/tourism"),
        redis_url="redis://redis:6379/0",
    )
    with pytest.raises(RuntimeError, match="placeholder credential"):
        validate_settings(settings)


def test_validate_settings_allows_local_password_in_development() -> None:
    settings = Settings(environment="development")
    validate_settings(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", SQLI_PAYLOADS)
async def test_places_search_treats_sqli_as_literal(live_app: object, payload: str) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/places", params={"q": payload, "limit": 5})
        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert body["limit"] == 5
        # Payload must not error the API or dump unrelated schema errors.
        assert isinstance(body["items"], list)


@pytest.mark.asyncio
async def test_places_rejects_oversized_query(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/places", params={"q": "x" * 201})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_places_rejects_excessive_limit(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/places", params={"limit": 101})
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_unpublished_or_missing_place_is_not_found(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/places/{uuid4()}")
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "place_not_found"
        assert "traceback" not in str(body).lower()


@pytest.mark.asyncio
async def test_malicious_sort_like_strings_in_q_do_not_500(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/places",
            params={"q": "name; DROP TABLE places--", "limit": 1},
        )
        assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", SQLI_PAYLOADS)
async def test_routes_search_treats_sqli_as_literal(live_app: object, payload: str) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/routes", params={"q": payload, "limit": 5})
        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert isinstance(body["items"], list)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", XSS_LIKE_PAYLOADS)
async def test_routes_and_places_accept_xss_like_search_as_data(
    live_app: object,
    payload: str,
) -> None:
    """XSS payloads are ordinary query strings; API must not 500 or execute anything."""
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        places = await client.get("/api/v1/places", params={"q": payload, "limit": 5})
        routes = await client.get("/api/v1/routes", params={"q": payload, "limit": 5})
        assert places.status_code == 200
        assert routes.status_code == 200
        assert isinstance(places.json()["items"], list)
        assert isinstance(routes.json()["items"], list)


@pytest.mark.asyncio
async def test_routes_rejects_oversized_query_and_limit(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/routes", params={"q": "x" * 201})).status_code == 422
        assert (await client.get("/api/v1/routes", params={"limit": 101})).status_code == 422


@pytest.mark.asyncio
async def test_missing_route_is_not_found(live_app: object) -> None:
    transport = ASGITransport(app=live_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/v1/routes/{uuid4()}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "route_not_found"
        assert "traceback" not in str(response.json()).lower()
