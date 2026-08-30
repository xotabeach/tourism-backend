"""Route execution lifecycle and ownership regressions."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
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
        json={"display_name": "Испытатель маршрута", "phone": phone},
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


async def _catalog_route(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.get("/api/v1/routes", params={"limit": 1})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items, "seeded catalog must contain a route"
    return items[0]["id"], items[0]


@pytest.mark.asyncio
async def test_route_execution_lifecycle_is_idempotent(live_client: AsyncClient) -> None:
    tokens = await _login(live_client, f"+7900{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    route_id, _ = await _catalog_route(live_client)

    started = await live_client.post(
        "/api/v1/route-executions",
        json={"route_id": route_id},
        headers=headers,
    )
    assert started.status_code == 201, started.text
    execution = started.json()
    assert execution["status"] == "active"
    assert execution["route_id"] == route_id
    assert execution["total_stops"] >= 1
    assert execution["completed_stops"] == 0
    assert execution["routing"] is not None
    assert execution["routing"]["snapshot_id"]
    assert execution["routing"]["revision"] >= 1
    assert execution["routing"]["captured_at"]

    snapshot_engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        with pytest.raises(DBAPIError):
            async with snapshot_engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE route_routing_snapshots "
                        "SET total_duration_seconds = COALESCE(total_duration_seconds, 0) + 1 "
                        "WHERE id = :snapshot_id"
                    ),
                    {"snapshot_id": execution["routing"]["snapshot_id"]},
                )
    finally:
        await snapshot_engine.dispose()

    repeated = await live_client.post(
        "/api/v1/route-executions",
        json={"route_id": route_id},
        headers=headers,
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["id"] == execution["id"]
    assert repeated.json()["routing"]["snapshot_id"] == execution["routing"]["snapshot_id"]

    active = await live_client.get("/api/v1/route-executions/active", headers=headers)
    assert active.status_code == 200
    assert active.json()["id"] == execution["id"]

    required = [stop for stop in execution["stops"] if not stop["is_optional"]]
    if required:
        incomplete = await live_client.post(
            f"/api/v1/route-executions/{execution['id']}/complete",
            headers=headers,
        )
        assert incomplete.status_code == 409
        assert incomplete.json()["error"]["code"] == "required_stops_incomplete"

    for stop in execution["stops"]:
        completed = await live_client.put(
            f"/api/v1/route-executions/{execution['id']}/stops/{stop['id']}/complete",
            headers=headers,
        )
        assert completed.status_code == 200, completed.text

    finished = await live_client.post(
        f"/api/v1/route-executions/{execution['id']}/complete",
        headers=headers,
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "completed"
    assert finished.json()["completed_required_stops"] == finished.json()["required_stops"]

    # Finishing pays travel points, sized by the route's own effort.
    awarded = finished.json()["awarded_points"]
    assert awarded > 0
    me_after = await live_client.get("/api/v1/me", headers=headers)
    assert me_after.status_code == 200, me_after.text

    repeated_finish = await live_client.post(
        f"/api/v1/route-executions/{execution['id']}/complete",
        headers=headers,
    )
    assert repeated_finish.status_code == 200
    assert repeated_finish.json()["id"] == execution["id"]
    # A replayed complete must not pay out a second time.
    assert repeated_finish.json()["awarded_points"] == awarded

    history = await live_client.get("/api/v1/route-executions", headers=headers)
    assert history.status_code == 200
    assert history.json()["total"] >= 1
    assert any(item["id"] == execution["id"] for item in history.json()["items"])

    no_active = await live_client.get("/api/v1/route-executions/active", headers=headers)
    assert no_active.status_code == 200
    assert no_active.json() is None


@pytest.mark.asyncio
async def test_route_execution_is_owner_scoped_and_cancel_is_idempotent(
    live_client: AsyncClient,
) -> None:
    owner_tokens = await _login(live_client, f"+7901{uuid4().int % 10_000_000:07d}")
    stranger_tokens = await _login(live_client, f"+7902{uuid4().int % 10_000_000:07d}")
    owner_headers = {"Authorization": f"Bearer {owner_tokens['access_token']}"}
    stranger_headers = {"Authorization": f"Bearer {stranger_tokens['access_token']}"}
    route_id, _ = await _catalog_route(live_client)

    started = await live_client.post(
        "/api/v1/route-executions",
        json={"route_id": route_id},
        headers=owner_headers,
    )
    assert started.status_code == 201, started.text
    execution_id = started.json()["id"]

    forbidden_get = await live_client.get(
        f"/api/v1/route-executions/{execution_id}",
        headers=stranger_headers,
    )
    assert forbidden_get.status_code == 404

    forbidden_stop = await live_client.put(
        f"/api/v1/route-executions/{execution_id}/stops/{started.json()['stops'][0]['id']}/complete",
        headers=stranger_headers,
    )
    assert forbidden_stop.status_code == 404

    cancelled = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/cancel",
        headers=owner_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    repeated_cancel = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/cancel",
        headers=owner_headers,
    )
    assert repeated_cancel.status_code == 200
    assert repeated_cancel.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_route_execution_requires_auth(live_client: AsyncClient) -> None:
    response = await live_client.get("/api/v1/route-executions")
    assert response.status_code == 401
