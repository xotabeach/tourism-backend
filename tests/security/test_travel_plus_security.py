"""Travel+ subscription security regressions."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.subscriptions.infrastructure.models import TravelPlusSubscription

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
async def live_client() -> AsyncIterator[AsyncClient]:
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
        yield client
    await app.state.redis.aclose()
    await app.state.engine.dispose()


async def _login(client: AsyncClient, phone: str, name: str = "Подписчик") -> dict:
    req = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": name, "phone": phone},
    )
    assert req.status_code == 204, req.text
    verify = await client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": "1234",
            "privacy_accepted": True,
            "personal_data_accepted": True,
        },
    )
    assert verify.status_code == 200, verify.text
    return verify.json()


@pytest.mark.asyncio
async def test_travel_plus_purchase_is_locked_and_me_has_beta_ai_fields(
    live_client: AsyncClient,
) -> None:
    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    me = await live_client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    body = me.json()
    assert body["travel_plus_active"] is False
    assert body["travel_plus_plan"] is None
    assert body["travel_plus_expires_at"] is None
    assert body["ai_chat_enabled"] is True
    assert body["max_route_points"] == 12
    assert body["alternatives_count"] == 3

    bad = await live_client.post(
        "/api/v1/me/travel-plus/activate",
        headers=headers,
        json={"plan": "lifetime"},
    )
    assert bad.status_code == 422

    oversized = await live_client.post(
        "/api/v1/me/travel-plus/activate",
        headers=headers,
        json={"plan": "monthly", "card_number": "4111111111111111"},
    )
    assert oversized.status_code == 422

    denied = await live_client.post(
        "/api/v1/me/travel-plus/activate",
        headers=headers,
        json={"plan": "monthly"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "travel_plus_purchase_unavailable"

    unchanged = await live_client.get("/api/v1/me", headers=headers)
    assert unchanged.json()["travel_plus_active"] is False


@pytest.mark.asyncio
async def test_travel_plus_activate_requires_auth(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/me/travel-plus/activate",
        json={"plan": "yearly"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_expires_stale_travel_plus(live_client: AsyncClient) -> None:
    phone = f"+7907{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone, name="Истёкший")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    user_id = me.json()["id"]

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(UTC)
    async with session_factory() as session:
        user = await session.get(User, UUID(user_id))
        assert user is not None
        user.travel_plus_active = True
        user.travel_plus_plan = "monthly"
        user.travel_plus_expires_at = now - timedelta(minutes=1)
        session.add(
            TravelPlusSubscription(
                id=uuid4(),
                user_id=user.id,
                plan="monthly",
                status="active",
                starts_at=now - timedelta(days=31),
                ends_at=now - timedelta(minutes=1),
                source="admin",
                created_at=now - timedelta(days=31),
                updated_at=now - timedelta(days=31),
            )
        )
        await session.commit()
    await engine.dispose()

    refreshed = await live_client.get("/api/v1/me", headers=headers)
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["travel_plus_active"] is False
    assert body["travel_plus_plan"] is None
    assert body["travel_plus_expires_at"] is None


@pytest.mark.asyncio
async def test_travel_plus_activate_cannot_be_enabled_by_environment_patch(
    live_client: AsyncClient,
) -> None:
    """The public endpoint fails closed without consulting APP_ENV."""
    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    denied = await live_client.post(
        "/api/v1/me/travel-plus/activate",
        headers=headers,
        json={"plan": "monthly"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "travel_plus_purchase_unavailable"
