"""Blocking a person and who may invite me (BACKEND-58, spec 11b D15)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
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


async def _login(client: AsyncClient, phone: str, name: str = "Тестер") -> dict:
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


async def _user(client: AsyncClient, name: str) -> tuple[str, dict[str, str]]:
    tokens = await _login(client, phone=f"+7906{uuid4().int % 10_000_000:07d}", name=name)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await client.get("/api/v1/me", headers=headers)
    return me.json()["id"], headers


async def _follow(client: AsyncClient, headers: dict[str, str], user_id: str) -> None:
    response = await client.put(f"/api/v1/users/{user_id}/like", headers=headers)
    assert response.status_code == 204, response.text


def _ids(response) -> list[str]:  # type: ignore[no-untyped-def]
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


@pytest.mark.asyncio
async def test_only_mutual_followers_who_accept_invites_are_invitable(
    live_client: AsyncClient,
) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    boris, boris_h = await _user(live_client, "Борис")
    vera, vera_h = await _user(live_client, "Вера")
    await _follow(live_client, anna_h, boris)
    await _follow(live_client, anna_h, vera)
    await _follow(live_client, vera_h, anna)

    # Boris does not follow back: only Vera is a mutual follower.
    assert _ids(await live_client.get("/api/v1/users/invitable", headers=anna_h)) == [vera]

    # Vera closes invites.
    patch = await live_client.patch("/api/v1/me", headers=vera_h, json={"invite_policy": "nobody"})
    assert patch.status_code == 200, patch.text
    assert patch.json()["invite_policy"] == "nobody"
    assert _ids(await live_client.get("/api/v1/users/invitable", headers=anna_h)) == []
    await live_client.patch("/api/v1/me", headers=vera_h, json={"invite_policy": "mutual"})
    assert _ids(await live_client.get("/api/v1/users/invitable", headers=anna_h)) == [vera]
    assert boris_h


@pytest.mark.asyncio
async def test_a_block_works_both_ways_and_can_be_lifted(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    await _follow(live_client, anna_h, vera)
    await _follow(live_client, vera_h, anna)

    block = await live_client.put(f"/api/v1/users/{anna}/block", headers=vera_h)
    assert block.status_code == 204, block.text
    # Repeating it is harmless.
    assert (await live_client.put(f"/api/v1/users/{anna}/block", headers=vera_h)).status_code == 204

    assert _ids(await live_client.get("/api/v1/users/invitable", headers=anna_h)) == []
    assert _ids(await live_client.get("/api/v1/users/invitable", headers=vera_h)) == []
    assert _ids(await live_client.get("/api/v1/users/blocked", headers=vera_h)) == [anna]
    assert _ids(await live_client.get("/api/v1/users/blocked", headers=anna_h)) == []

    profile = await live_client.get(f"/api/v1/users/{anna}", headers=vera_h)
    assert profile.json()["blocked_by_me"] is True
    other_side = await live_client.get(f"/api/v1/users/{vera}", headers=anna_h)
    assert other_side.json()["blocked_by_me"] is False

    lift = await live_client.delete(f"/api/v1/users/{anna}/block", headers=vera_h)
    assert lift.status_code == 204, lift.text
    assert _ids(await live_client.get("/api/v1/users/invitable", headers=anna_h)) == [vera]


@pytest.mark.asyncio
async def test_blocking_needs_a_session_and_not_oneself(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    assert (await live_client.put(f"/api/v1/users/{anna}/block")).status_code == 401
    assert (await live_client.get("/api/v1/users/blocked")).status_code == 401
    itself = await live_client.put(f"/api/v1/users/{anna}/block", headers=anna_h)
    assert itself.status_code == 400
    missing = await live_client.put(f"/api/v1/users/{uuid4()}/block", headers=anna_h)
    assert missing.status_code == 404
