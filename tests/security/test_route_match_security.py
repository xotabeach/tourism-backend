"""Security regressions for POST /route-builder/match."""

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
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield client
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()


async def _login(client: AsyncClient, phone: str, name: str = "Матчер") -> dict:
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
async def test_match_requires_auth(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/route-builder/match",
        json={"city": "Ялта"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_match_rejects_oversized_and_extra_fields(live_client: AsyncClient) -> None:
    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    oversized = await live_client.post(
        "/api/v1/route-builder/match",
        headers=headers,
        json={"city": "Я" * 100},
    )
    assert oversized.status_code == 422

    extra = await live_client.post(
        "/api/v1/route-builder/match",
        headers=headers,
        json={"city": "Ялта", "role": "admin"},
    )
    assert extra.status_code == 422

    sqli = await live_client.post(
        "/api/v1/route-builder/match",
        headers=headers,
        json={"city": "Ялта'; DROP TABLE routes;--", "interests": ["Природа"]},
    )
    assert sqli.status_code == 200
    body = sqli.json()
    assert body["strategy"] == "algorithmic"
    assert "ideal" in body
    assert "close" in body
    assert isinstance(body["offer_generate"], bool)


@pytest.mark.asyncio
async def test_match_returns_ranked_bands(live_client: AsyncClient) -> None:
    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = await live_client.post(
        "/api/v1/route-builder/match",
        headers=headers,
        json={
            "city": "Ялта",
            "trip_type": "rest",
            "duration": "d3_5",
            "people": 2,
            "interests": ["Пляж", "Природа"],
            "pace": "calm",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scored_total"] >= 1
    assert body["ai_rerank_applied"] is False
    for hit in body["ideal"] + body["close"]:
        assert 0.0 <= hit["score"] <= 1.0
        assert hit["band"] in {"ideal", "close"}
        assert hit["route"]["name"]
        assert "<script>" not in hit["route"]["name"]
