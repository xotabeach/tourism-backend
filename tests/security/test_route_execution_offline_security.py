"""Offline replay contract for route executions: dedupe, bounds, ownership."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
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


async def _login(client: AsyncClient, prefix: str) -> dict[str, str]:
    phone = f"{prefix}{uuid4().int % 10_000_000:07d}"
    requested = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Офлайн-турист", "phone": phone},
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


async def _start_execution(client: AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    catalog = await client.get("/api/v1/routes", params={"limit": 1})
    assert catalog.status_code == 200, catalog.text
    items = catalog.json()["items"]
    assert items, "seeded catalog must contain a route"
    started = await client.post(
        "/api/v1/route-executions",
        json={"route_id": items[0]["id"]},
        headers=headers,
    )
    assert started.status_code == 201, started.text
    return started.json()


@pytest.mark.asyncio
async def test_replayed_stop_event_is_recorded_once(live_client: AsyncClient) -> None:
    tokens = await _login(live_client, "+7903")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    execution = await _start_execution(live_client, headers)
    stop = execution["stops"][0]  # type: ignore[index]
    # The queue entry reports the moment the phone recorded it, which is
    # earlier than this request but never earlier than the run itself.
    started_at = datetime.fromisoformat(str(execution["started_at"]))
    occurred_at = started_at
    client_event_id = str(uuid4())
    body = {"client_event_id": client_event_id, "occurred_at": occurred_at.isoformat()}

    first = await live_client.put(
        f"/api/v1/route-executions/{execution['id']}/stops/{stop['id']}/complete",
        json=body,
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["sync"]["replayed"] is False
    assert first.json()["sync"]["applied"] is True
    assert first.json()["sync"]["action"] == "complete_stop"
    assert datetime.fromisoformat(first.json()["sync"]["occurred_at"]) == occurred_at
    first_completed_at = next(
        item["completed_at"] for item in first.json()["stops"] if item["id"] == stop["id"]
    )
    # Server time has moved on since the run started, so an equal timestamp
    # proves the reported moment was kept instead of "now".
    assert datetime.fromisoformat(first_completed_at) == occurred_at

    replay = await live_client.put(
        f"/api/v1/route-executions/{execution['id']}/stops/{stop['id']}/complete",
        json=body,
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["sync"]["replayed"] is True
    assert replay.json()["sync"]["client_event_id"] == client_event_id
    replay_completed_at = next(
        item["completed_at"] for item in replay.json()["stops"] if item["id"] == stop["id"]
    )
    assert replay_completed_at == first_completed_at
    assert replay.json()["completed_stops"] == first.json()["completed_stops"]


@pytest.mark.asyncio
async def test_offline_time_is_bounded_and_clamped(live_client: AsyncClient) -> None:
    tokens = await _login(live_client, "+7904")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    execution = await _start_execution(live_client, headers)
    stop = execution["stops"][0]  # type: ignore[index]
    started_at = datetime.fromisoformat(str(execution["started_at"]))
    stop_url = f"/api/v1/route-executions/{execution['id']}/stops/{stop['id']}/complete"

    future = await live_client.put(
        stop_url,
        json={
            "client_event_id": str(uuid4()),
            "occurred_at": (started_at + timedelta(days=2)).isoformat(),
        },
        headers=headers,
    )
    assert future.status_code == 422, future.text
    assert future.json()["error"]["code"] == "route_execution_event_time_invalid"

    unparsable = await live_client.put(
        stop_url,
        json={"client_event_id": str(uuid4()), "occurred_at": "1 OR 1=1; DROP TABLE routes"},
        headers=headers,
    )
    assert unparsable.status_code == 422, unparsable.text

    forged_key = await live_client.put(
        stop_url,
        json={"client_event_id": "'; DELETE FROM route_executions; --"},
        headers=headers,
    )
    assert forged_key.status_code == 422, forged_key.text

    unknown_field = await live_client.put(
        stop_url,
        json={"client_event_id": str(uuid4()), "user_id": str(uuid4())},
        headers=headers,
    )
    assert unknown_field.status_code == 422, unknown_field.text

    # A stop cannot be finished before the run that contains it started.
    clamped = await live_client.put(
        stop_url,
        json={
            "client_event_id": str(uuid4()),
            "occurred_at": (started_at - timedelta(days=1)).isoformat(),
        },
        headers=headers,
    )
    assert clamped.status_code == 200, clamped.text
    completed_at = next(
        item["completed_at"] for item in clamped.json()["stops"] if item["id"] == stop["id"]
    )
    assert datetime.fromisoformat(completed_at) == started_at

    catalog_alive = await live_client.get("/api/v1/routes", params={"limit": 1})
    assert catalog_alive.status_code == 200
    assert catalog_alive.json()["items"], "injection-like input must stay plain data"


@pytest.mark.asyncio
async def test_queue_replay_after_terminal_state_is_honest(live_client: AsyncClient) -> None:
    tokens = await _login(live_client, "+7905")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    execution = await _start_execution(live_client, headers)
    stops = list(execution["stops"])  # type: ignore[arg-type]

    for stop in stops:
        completed = await live_client.put(
            f"/api/v1/route-executions/{execution['id']}/stops/{stop['id']}/complete",
            json={"client_event_id": str(uuid4())},
            headers=headers,
        )
        assert completed.status_code == 200, completed.text

    finished = await live_client.post(
        f"/api/v1/route-executions/{execution['id']}/complete",
        json={"client_event_id": str(uuid4())},
        headers=headers,
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "completed"

    # A queued duplicate for a stop the finished run already holds is not an
    # error: the client may drop it instead of retrying forever.
    late_stop = await live_client.put(
        f"/api/v1/route-executions/{execution['id']}/stops/{stops[0]['id']}/complete",
        json={"client_event_id": str(uuid4())},
        headers=headers,
    )
    assert late_stop.status_code == 200, late_stop.text
    assert late_stop.json()["status"] == "completed"

    late_cancel = await live_client.post(
        f"/api/v1/route-executions/{execution['id']}/cancel",
        json={"client_event_id": str(uuid4())},
        headers=headers,
    )
    assert late_cancel.status_code == 409, late_cancel.text
    assert late_cancel.json()["error"]["details"]["retryable"] is False
    assert late_cancel.json()["error"]["details"]["status"] == "completed"


@pytest.mark.asyncio
async def test_client_event_id_is_scoped_to_its_owner(live_client: AsyncClient) -> None:
    owner_tokens = await _login(live_client, "+7906")
    stranger_tokens = await _login(live_client, "+7907")
    owner_headers = {"Authorization": f"Bearer {owner_tokens['access_token']}"}
    stranger_headers = {"Authorization": f"Bearer {stranger_tokens['access_token']}"}
    execution = await _start_execution(live_client, owner_headers)
    stop = execution["stops"][0]  # type: ignore[index]
    shared_event_id = str(uuid4())
    stop_url = f"/api/v1/route-executions/{execution['id']}/stops/{stop['id']}/complete"

    owned = await live_client.put(
        stop_url,
        json={"client_event_id": shared_event_id},
        headers=owner_headers,
    )
    assert owned.status_code == 200, owned.text

    # Reusing another user's event id must not leak the run or its state.
    stolen = await live_client.put(
        stop_url,
        json={"client_event_id": shared_event_id},
        headers=stranger_headers,
    )
    assert stolen.status_code == 404, stolen.text

    stranger_execution = await _start_execution(live_client, stranger_headers)
    assert stranger_execution["id"] != execution["id"]


@pytest.mark.asyncio
async def test_offline_events_require_auth(live_client: AsyncClient) -> None:
    response = await live_client.post(
        f"/api/v1/route-executions/{uuid4()}/cancel",
        json={"client_event_id": str(uuid4())},
    )
    assert response.status_code == 401
