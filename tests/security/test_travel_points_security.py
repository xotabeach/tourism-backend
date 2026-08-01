"""Profile likes + delayed travel-point awards."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.identity.application.travel_points import (
    AWARD_DELAY,
    AWARD_POINTS,
    grant_due_travel_points,
)
from tourism_backend.modules.identity.infrastructure.models import ProfileLike, User

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
            await conn.execute(text("SELECT travel_points FROM users LIMIT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


async def _otp_login(client: AsyncClient, phone: str, name: str) -> tuple[str, str]:
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
    payload = verify.json()
    token = payload["access_token"]
    me = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    return token, me.json()["id"]


@pytest.fixture
async def points_client() -> AsyncIterator[AsyncClient]:
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
        auth_otp_store_debug_code=True,
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
        admin_enabled=False,
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await app.state.redis.aclose()
    await app.state.engine.dispose()


@pytest.mark.asyncio
async def test_cannot_like_own_profile(points_client: AsyncClient) -> None:
    phone = f"+7905{uuid4().int % 10_000_000:07d}"
    token, user_id = await _otp_login(points_client, phone, "Self")
    resp = await points_client.put(
        f"/api/v1/users/{user_id}/like",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_like_requires_auth_and_rejects_injection_path(
    points_client: AsyncClient,
) -> None:
    bare = await points_client.put("/api/v1/users/not-a-uuid/like")
    assert bare.status_code in {401, 422}
    evil = await points_client.put(
        "/api/v1/users/00000000-0000-0000-0000-000000000001%27%20OR%201%3D1/like",
    )
    assert evil.status_code in {401, 422}


@pytest.mark.asyncio
async def test_profile_like_awards_after_delay(points_client: AsyncClient) -> None:
    phone_a = f"+7906{uuid4().int % 10_000_000:07d}"
    phone_b = f"+7907{uuid4().int % 10_000_000:07d}"
    token_a, _alice_id = await _otp_login(points_client, phone_a, "Alice")
    _token_b, bob_id = await _otp_login(points_client, phone_b, "Bob")

    like = await points_client.put(
        f"/api/v1/users/{bob_id}/like",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert like.status_code == 204, like.text

    profile = await points_client.get(
        f"/api/v1/users/{bob_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert profile.status_code == 200
    assert profile.json()["liked_by_me"] is True
    assert profile.json()["travel_points"] == 0

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        row = await session.get(
            ProfileLike,
            (
                (
                    await session.execute(select(User.id).where(User.phone_e164 == phone_a))
                ).scalar_one(),
                (
                    await session.execute(select(User.id).where(User.phone_e164 == phone_b))
                ).scalar_one(),
            ),
        )
        assert row is not None
        row.created_at = datetime.now(UTC) - AWARD_DELAY - timedelta(minutes=1)
        await session.commit()
        granted = await grant_due_travel_points(session)
        assert granted >= 1
        bob = (await session.execute(select(User).where(User.phone_e164 == phone_b))).scalar_one()
        assert bob.travel_points >= AWARD_POINTS
    await engine.dispose()

    # Unlike after award does not claw back.
    unlike = await points_client.delete(
        f"/api/v1/users/{bob_id}/like",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert unlike.status_code == 204
    profile2 = await points_client.get(f"/api/v1/users/{bob_id}")
    assert profile2.json()["travel_points"] >= AWARD_POINTS
