"""The swiper must not go blank when a user has consumed the catalogue.

Reproduces the production case behind the empty deck: an active user with
almost every eligible route saved or skipped.
"""

import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.favorites.infrastructure.models import FavoriteRoute

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


async def _deps_available() -> bool:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def context() -> AsyncIterator[tuple[AsyncClient, Any]]:
    if not await _deps_available():
        if os.getenv("CI") or os.getenv("REQUIRE_INTEGRATION_DEPS") == "1":
            pytest.fail("Postgres/Redis required for integration tests are unavailable")
        pytest.skip("Postgres/Redis for integration tests are unavailable")
    settings = Settings(
        app_env="test",
        database_url=DATABASE_URL,
        database_url_sync=DATABASE_URL.replace("+asyncpg", "+psycopg"),
        redis_url=REDIS_URL,
        auth_otp_accept_any=True,
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app
    await app.state.redis.aclose()
    await app.state.engine.dispose()


async def _login(client: AsyncClient) -> dict[str, Any]:
    phone = f"+7909{uuid4().int % 10_000_000:07d}"
    requested = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Активный турист", "phone": phone},
    )
    assert requested.status_code == 204, requested.text
    verified = await client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": "1234",
            "privacy_accepted": True,
            "personal_data_accepted": True,
        },
    )
    assert verified.status_code == 200, verified.text
    return verified.json()


@pytest.mark.asyncio
async def test_deck_falls_back_to_catalog_when_everything_is_favourited(
    context: tuple[AsyncClient, Any],
) -> None:
    client, app = context
    tokens = await _login(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200, me.text
    user_id = me.json()["id"]

    routes = await client.get("/api/v1/routes", params={"limit": 100})
    assert routes.status_code == 200, routes.text
    route_ids = [item["id"] for item in routes.json()["items"]]
    assert route_ids, "seeded catalogue is required for this test"

    try:
        for route_id in route_ids:
            saved = await client.put(
                f"/api/v1/favorites/routes/{route_id}",
                headers=headers,
            )
            assert saved.status_code in (200, 201, 204), saved.text

        deck = await client.get("/api/v1/routes/recommendations/today", headers=headers)
        assert deck.status_code == 200, deck.text
        body = deck.json()

        # Every personalised candidate is excluded, so without the fallback
        # this list was empty and the swiper rendered nothing.
        assert body["items"], "deck must not be empty when the catalogue is non-empty"
        assert all(item["explanation_code"] == "catalog_fallback" for item in body["items"])
    finally:
        async with app.state.session_factory() as session:
            await session.execute(
                delete(FavoriteRoute).where(FavoriteRoute.user_id == UUID(user_id))
            )
            await session.commit()
