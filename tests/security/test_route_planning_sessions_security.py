"""Security regressions for route-builder AI chat sessions (Phase 8B)."""

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
        ai_planning_enabled=False,
        ai_provider="mock",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            yield client
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()


async def _login(client: AsyncClient, phone: str, name: str = "Чат") -> dict:
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


async def _travel_plus(client: AsyncClient, headers: dict[str, str]) -> None:
    activated = await client.post(
        "/api/v1/me/travel-plus/activate",
        headers=headers,
        json={"plan": "monthly"},
    )
    assert activated.status_code == 200, activated.text


@pytest.mark.asyncio
async def test_sessions_require_auth_and_travel_plus(live_client: AsyncClient) -> None:
    unauth = await live_client.post(
        "/api/v1/route-builder/sessions",
        json={"params": {"city": "Ялта"}},
    )
    assert unauth.status_code == 401

    phone = f"+7907{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    blocked = await live_client.post(
        "/api/v1/route-builder/sessions",
        headers=headers,
        json={"params": {"city": "Ялта"}},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "travel_plus_required"

    await _travel_plus(live_client, headers)
    created = await live_client.post(
        "/api/v1/route-builder/sessions",
        headers=headers,
        json={"params": {"city": "Ялта", "interests": ["Пляж"]}},
    )
    assert created.status_code == 200, created.text
    assert created.json()["constraints"]["city"] == "Ялта"


@pytest.mark.asyncio
async def test_session_bola_and_message_bounds(live_client: AsyncClient) -> None:
    phone_a = f"+7907{uuid4().int % 10_000_000:07d}"
    phone_b = f"+7907{uuid4().int % 10_000_000:07d}"
    tokens_a = await _login(live_client, phone_a, name="Алиса")
    tokens_b = await _login(live_client, phone_b, name="Борис")
    headers_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {tokens_b['access_token']}"}
    await _travel_plus(live_client, headers_a)
    await _travel_plus(live_client, headers_b)

    created = await live_client.post(
        "/api/v1/route-builder/sessions",
        headers=headers_a,
        json={"params": {"city": "Ялта"}},
    )
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    foreign = await live_client.get(
        f"/api/v1/route-builder/sessions/{session_id}",
        headers=headers_b,
    )
    assert foreign.status_code == 404

    foreign_msg = await live_client.post(
        f"/api/v1/route-builder/sessions/{session_id}/messages",
        headers=headers_b,
        json={"text": "привет из чужой сессии"},
    )
    assert foreign_msg.status_code == 404

    oversized = await live_client.post(
        f"/api/v1/route-builder/sessions/{session_id}/messages",
        headers=headers_a,
        json={"text": "Я" * 2500},
    )
    assert oversized.status_code == 422

    sqli = await live_client.post(
        f"/api/v1/route-builder/sessions/{session_id}/messages",
        headers=headers_a,
        json={"text": "'; DROP TABLE users;-- хочу маршрут"},
    )
    assert sqli.status_code == 200, sqli.text
    assert "маршрут" in sqli.json()["text"].casefold() or sqli.json()["intent"]

    xss = await live_client.post(
        f"/api/v1/route-builder/sessions/{session_id}/messages",
        headers=headers_a,
        json={"text": "<script>alert(1)</script> спокойный темп в Ялте"},
    )
    assert xss.status_code == 200
    assert "<script>" not in (xss.json().get("blocks") or [])

    greeting = await live_client.post(
        f"/api/v1/route-builder/sessions/{session_id}/messages",
        headers=headers_a,
        json={"text": "привет"},
    )
    assert greeting.status_code == 200
    assert greeting.json()["intent"] == "greeting"
    assert greeting.json()["proposal"] is None

    generate = await live_client.post(
        f"/api/v1/route-builder/sessions/{session_id}/messages",
        headers=headers_a,
        json={"text": "подбери маршрут"},
    )
    assert generate.status_code == 200, generate.text
    body = generate.json()
    assert body["intent"] == "generate"
    assert body["proposal"] is not None
    assert body["proposal"]["proposal_id"]
