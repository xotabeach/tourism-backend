"""Security regressions for personalized route recommendations."""

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

SQLI_LIKE = "' OR '1'='1"
XSS_LIKE = "<script>alert(1)</script>"


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


async def _login(client: AsyncClient, phone: str) -> dict[str, str]:
    requested = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Колода", "phone": phone},
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


def _auth(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.mark.asyncio
async def test_recommendations_require_auth(live_client: AsyncClient) -> None:
    today = await live_client.get("/api/v1/routes/recommendations/today")
    assert today.status_code == 401
    assert "access_token" not in today.text.lower() or "error" in today.text

    skip = await live_client.post(
        f"/api/v1/routes/{uuid4()}/recommendation-feedback",
        json={"action": "skip", "client_event_id": str(uuid4())},
    )
    assert skip.status_code == 401


@pytest.mark.asyncio
async def test_feedback_rejects_unknown_action_and_oversized_payload(
    live_client: AsyncClient,
) -> None:
    tokens = await _login(live_client, f"+7900{uuid4().int % 10_000_000:07d}")
    headers = _auth(tokens)
    listed = await live_client.get("/api/v1/routes", params={"limit": 1})
    assert listed.status_code == 200
    items = listed.json()["items"]
    if not items:
        pytest.skip("no published routes in local catalog")
    route_id = items[0]["id"]

    unknown = await live_client.post(
        f"/api/v1/routes/{route_id}/recommendation-feedback",
        headers=headers,
        json={"action": "delete_all", "client_event_id": str(uuid4())},
    )
    assert unknown.status_code == 422

    sqli = await live_client.post(
        f"/api/v1/routes/{route_id}/recommendation-feedback",
        headers=headers,
        json={"action": SQLI_LIKE, "client_event_id": str(uuid4())},
    )
    assert sqli.status_code == 422

    extra = await live_client.post(
        f"/api/v1/routes/{route_id}/recommendation-feedback",
        headers=headers,
        json={
            "action": "skip",
            "client_event_id": str(uuid4()),
            "owner_user_id": str(uuid4()),
        },
    )
    assert extra.status_code == 422


@pytest.mark.asyncio
async def test_skip_is_owner_scoped_idempotent_and_hides_card(
    live_client: AsyncClient,
) -> None:
    tokens = await _login(live_client, f"+7900{uuid4().int % 10_000_000:07d}")
    headers = _auth(tokens)
    deck = await live_client.get("/api/v1/routes/recommendations/today", headers=headers)
    assert deck.status_code == 200, deck.text
    body = deck.json()
    assert body["ranker_version"] == "v1"
    assert "phone" not in deck.text
    assert SQLI_LIKE not in deck.text
    if not body["items"]:
        pytest.skip("recommendation catalog is empty")
    first = body["items"][0]
    route_id = first["route"]["id"]
    event_id = str(uuid4())
    skip = await live_client.post(
        f"/api/v1/routes/{route_id}/recommendation-feedback",
        headers=headers,
        json={"action": "skip", "client_event_id": event_id},
    )
    assert skip.status_code == 200, skip.text
    assert skip.json()["replayed"] is False

    replay = await live_client.post(
        f"/api/v1/routes/{route_id}/recommendation-feedback",
        headers=headers,
        json={"action": "skip", "client_event_id": event_id},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["route_id"] == route_id

    refreshed = await live_client.get("/api/v1/routes/recommendations/today", headers=headers)
    assert refreshed.status_code == 200
    remaining_ids = {item["route"]["id"] for item in refreshed.json()["items"]}
    assert route_id not in remaining_ids


@pytest.mark.asyncio
async def test_unpublished_or_unknown_route_cannot_be_skipped(
    live_client: AsyncClient,
) -> None:
    tokens = await _login(live_client, f"+7900{uuid4().int % 10_000_000:07d}")
    headers = _auth(tokens)
    missing_id = uuid4()
    skip = await live_client.post(
        f"/api/v1/routes/{missing_id}/recommendation-feedback",
        headers=headers,
        json={"action": "skip", "client_event_id": str(uuid4())},
    )
    assert skip.status_code == 404
    assert skip.json()["error"]["code"] == "route_not_found"

    deck = await live_client.get("/api/v1/routes/recommendations/today", headers=headers)
    assert deck.status_code == 200
    dumped = deck.text
    assert str(missing_id) not in dumped
    assert XSS_LIKE not in dumped
    for item in deck.json()["items"]:
        assert item["route"]["publication_status"] == "published"
        assert item["route"]["visibility"] == "public"
