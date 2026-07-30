"""Public user profile + attachment ownership security regressions."""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import text
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
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    app.state.engine = engine
    app.state.session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    app.state.redis = create_redis_client(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await app.state.redis.aclose()
    await engine.dispose()


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


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_public_profile_hides_phone_and_is_readable(
    live_client: AsyncClient,
) -> None:
    phone = f"+7905{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone, name="Публичный")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    user_id = me.json()["id"]

    upload = await live_client.post(
        "/api/v1/me/avatar",
        headers=headers,
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 200, upload.text

    public = await live_client.get(f"/api/v1/users/{user_id}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["id"] == user_id
    assert body["display_name"] == "Публичный"
    assert body["avatar_url"]
    assert "phone" not in body
    assert set(body.keys()) == {"id", "display_name", "avatar_url", "cover_url"}


@pytest.mark.asyncio
async def test_public_user_not_found(live_client: AsyncClient) -> None:
    missing = await live_client.get(f"/api/v1/users/{uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_routes_catalog_includes_owner_fields_when_present(
    live_client: AsyncClient,
) -> None:
    response = await live_client.get("/api/v1/routes", params={"region_slug": "crimea", "limit": 5})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "items" in payload
    for item in payload["items"]:
        assert "owner_user_id" in item
        assert "author_avatar_url" in item
        assert "author_label" in item


@pytest.mark.asyncio
async def test_public_user_routes_endpoint(live_client: AsyncClient) -> None:
    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone, name="Автор")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    user_id = me.json()["id"]

    routes = await live_client.get(f"/api/v1/users/{user_id}/routes")
    assert routes.status_code == 200, routes.text
    body = routes.json()
    assert "items" in body
    assert body["total"] >= 0
    for item in body["items"]:
        assert item.get("owner_user_id") == user_id
