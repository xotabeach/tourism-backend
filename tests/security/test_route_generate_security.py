"""Security regressions for route-builder generate + quotas."""

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


async def _login(client: AsyncClient, phone: str, name: str = "Генератор") -> dict:
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
async def test_generate_form_creates_draft_and_enforces_auth(
    live_client: AsyncClient,
) -> None:
    unauth = await live_client.post(
        "/api/v1/route-builder/generate",
        json={"channel": "form", "params": {"city": "Ялта"}},
    )
    assert unauth.status_code == 401

    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    oversized = await live_client.post(
        "/api/v1/route-builder/generate",
        headers=headers,
        json={"channel": "form", "params": {"city": "Я" * 200}},
    )
    assert oversized.status_code == 422

    response = await live_client.post(
        "/api/v1/route-builder/generate",
        headers=headers,
        json={
            "channel": "form",
            "params": {
                "city": "Ялта",
                "duration": "d3_5",
                "interests": ["Пляж", "Природа"],
                "pace": "calm",
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channel"] == "form"
    assert body["persisted_draft"] is True
    assert body["route_id"]
    assert body["proposal"]["status"] == "accepted"
    assert body["proposal"]["quota"]["weekly_used"] >= 1
    assert any(block["type"] == "route_proposal_card" for block in body["proposal"]["blocks"])


@pytest.mark.asyncio
async def test_chat_generate_requires_travel_plus_then_accept(
    live_client: AsyncClient,
) -> None:
    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone, name="БезПлюса")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    blocked = await live_client.post(
        "/api/v1/route-builder/generate",
        headers=headers,
        json={"channel": "chat", "params": {"city": "Ялта"}},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "travel_plus_required"

    activated = await live_client.post(
        "/api/v1/me/travel-plus/activate",
        headers=headers,
        json={"plan": "monthly"},
    )
    assert activated.status_code == 200

    generated = await live_client.post(
        "/api/v1/route-builder/generate",
        headers=headers,
        json={
            "channel": "chat",
            "params": {
                "city": "Ялта",
                "season": "лето",
                "transport_mode": "car",
                "day_kind": "weekend",
                "budget_amount": 5000,
                "interests": ["История"],
            },
        },
    )
    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert body["persisted_draft"] is False
    assert body["route_id"] is None
    assert body["proposal"]["status"] == "draft"
    proposal_id = body["proposal"]["proposal_id"]

    accepted = await live_client.post(
        f"/api/v1/route-builder/proposals/{proposal_id}/accept",
        headers=headers,
    )
    assert accepted.status_code == 200
    accepted_body = accepted.json()
    assert accepted_body["status"] == "accepted"
    assert accepted_body["route_id"]

    # Idempotent accept
    again = await live_client.post(
        f"/api/v1/route-builder/proposals/{proposal_id}/accept",
        headers=headers,
    )
    assert again.status_code == 200
    assert again.json()["route_id"] == accepted_body["route_id"]
