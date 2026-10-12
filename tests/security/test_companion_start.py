"""The group's start and runs started by each phone (BACKEND-60, spec 11b)."""

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


async def _friends(
    client: AsyncClient, a: dict[str, str], a_id: str, b: dict[str, str], b_id: str
) -> None:
    await _follow(client, a, b_id)
    await _follow(client, b, a_id)


async def _route_id(client: AsyncClient) -> str:
    response = await client.get("/api/v1/routes", params={"limit": 1})
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["id"]


async def _group(client: AsyncClient, headers: dict[str, str]) -> dict:
    response = await client.post(
        "/api/v1/companion-groups", headers=headers, json={"route_id": await _route_id(client)}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _sql(statement: str, **params: object) -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)
    await engine.dispose()


RUNS = "/api/v1/route-executions"


async def _started_pair(client: AsyncClient):  # type: ignore[no-untyped-def]
    """Anna organizes, Vera joins and is ready; Anna gives the start."""
    anna, anna_h = await _user(client, "Анна")
    vera, vera_h = await _user(client, "Вера")
    await _friends(client, anna_h, anna, vera_h, vera)
    group = await _group(client, anna_h)
    url = f"/api/v1/companion-groups/{group['id']}"
    await client.post(f"{url}/invites", headers=anna_h, json={"user_id": vera})
    await client.post(f"{url}/accept", headers=vera_h)
    ready = await client.post(f"{url}/ready", headers=vera_h, json={"ready": True})
    assert ready.status_code == 200, ready.text
    started = await client.post(f"{url}/start", headers=anna_h)
    assert started.status_code == 200, started.text
    return group, url, (anna, anna_h), (vera, vera_h), started.json()


def _member(group: dict, user_id: str) -> dict:
    return next(m for m in group["members"] if m["user_id"] == user_id)


@pytest.mark.asyncio
async def test_alone_there_is_no_start(live_client: AsyncClient) -> None:
    _anna, anna_h = await _user(live_client, "Анна")
    group = await _group(live_client, anna_h)
    alone = await live_client.post(f"/api/v1/companion-groups/{group['id']}/start", headers=anna_h)
    assert alone.status_code == 409
    assert alone.json()["error"]["code"] == "companion_group_alone"


@pytest.mark.asyncio
async def test_ready_waits_for_another_run_to_end(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    url = f"/api/v1/companion-groups/{group['id']}"
    await live_client.post(f"{url}/invites", headers=anna_h, json={"user_id": vera})
    await live_client.post(f"{url}/accept", headers=vera_h)
    solo = await live_client.post(RUNS, headers=vera_h, json={"route_id": group["route_id"]})
    assert solo.status_code == 201, solo.text
    busy = await live_client.post(f"{url}/ready", headers=vera_h, json={"ready": True})
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "companion_busy_run"
    await live_client.post(f"{RUNS}/{solo.json()['id']}/cancel", headers=vera_h)
    ok = await live_client.post(f"{url}/ready", headers=vera_h, json={"ready": True})
    assert ok.status_code == 200, ok.text
    assert _member(ok.json(), vera)["ready"] is True


@pytest.mark.asyncio
async def test_each_phone_starts_its_own_run_at_the_group_time(live_client: AsyncClient) -> None:
    group, url, (anna, anna_h), (vera, vera_h), started = await _started_pair(live_client)
    assert started["status"] == "started"
    assert started["started_at"]
    inbox = await live_client.get("/api/v1/me/notifications", headers=vera_h)
    assert any(n["kind"] == "companion_group_started" for n in inbox.json()["items"])

    # Nobody has a run yet: the server does not start one for anyone.
    assert not any(m["started"] for m in started["members"])

    # A phone clock a little behind: the run cannot begin before the group.
    group_start = datetime.fromisoformat(started["started_at"])
    run = await live_client.post(
        RUNS,
        headers=vera_h,
        json={
            "route_id": group["route_id"],
            "companion_group_id": group["id"],
            "occurred_at": (group_start - timedelta(minutes=10)).isoformat(),
        },
    )
    assert run.status_code == 201, run.text
    assert datetime.fromisoformat(run.json()["started_at"]) == group_start
    seen = (await live_client.get(url, headers=anna_h)).json()
    assert _member(seen, vera)["started"] is True
    assert _member(seen, vera)["joined_late"] is False
    assert _member(seen, anna)["started"] is False

    # Retrying the same start (a phone that started offline) is harmless.
    again = await live_client.post(
        RUNS,
        headers=vera_h,
        json={"route_id": group["route_id"], "companion_group_id": group["id"]},
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] == run.json()["id"]


@pytest.mark.asyncio
async def test_a_run_cannot_claim_a_group_it_does_not_belong_to(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    vera, vera_h = await _user(live_client, "Вера")
    await _friends(live_client, anna_h, anna, vera_h, vera)
    group = await _group(live_client, anna_h)
    url = f"/api/v1/companion-groups/{group['id']}"
    early = await live_client.post(
        RUNS,
        headers=anna_h,
        json={"route_id": group["route_id"], "companion_group_id": group["id"]},
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "companion_group_not_started"

    _gleb, gleb_h = await _user(live_client, "Глеб")
    stranger = await live_client.post(
        RUNS,
        headers=gleb_h,
        json={"route_id": group["route_id"], "companion_group_id": group["id"]},
    )
    assert stranger.status_code == 404
    assert url


@pytest.mark.asyncio
async def test_late_and_unready_walkers_are_marked(live_client: AsyncClient) -> None:
    group, url, (anna, anna_h), (_vera, _vera_h), started = await _started_pair(live_client)
    # Gleb asks in with the code after the start; the organizer lets him in.
    gleb, gleb_h = await _user(live_client, "Глеб")
    asked = await live_client.post(
        "/api/v1/companion-groups/join", headers=gleb_h, json={"code": group["join_code"]}
    )
    assert asked.status_code == 200, asked.text
    approved = await live_client.post(f"{url}/requests/{gleb}/approve", headers=anna_h)
    assert _member(approved.json(), gleb)["joined_late"] is True
    run = await live_client.post(
        RUNS,
        headers=gleb_h,
        json={"route_id": group["route_id"], "companion_group_id": group["id"]},
    )
    assert run.status_code == 201, run.text

    # Anna starts her own run long after her start: late as well.
    later = datetime.fromisoformat(started["started_at"]) + timedelta(minutes=40)
    await _sql(
        "UPDATE companion_groups SET started_at = started_at - interval '40 minutes' "
        "WHERE id = :id",
        id=group["id"],
    )
    own = await live_client.post(
        RUNS,
        headers=anna_h,
        json={"route_id": group["route_id"], "companion_group_id": group["id"]},
    )
    assert own.status_code == 201, own.text
    seen = (await live_client.get(url, headers=anna_h)).json()
    assert _member(seen, anna)["joined_late"] is True
    assert later


@pytest.mark.asyncio
async def test_a_start_time_from_the_future_is_refused(live_client: AsyncClient) -> None:
    group, _url, _anna, (_vera, vera_h), _started = await _started_pair(live_client)
    future = datetime.fromisoformat(_started["started_at"]) + timedelta(hours=2)
    run = await live_client.post(
        RUNS,
        headers=vera_h,
        json={
            "route_id": group["route_id"],
            "companion_group_id": group["id"],
            "occurred_at": future.isoformat(),
        },
    )
    assert run.status_code == 422
    assert run.json()["error"]["code"] == "route_execution_event_time_invalid"


@pytest.mark.asyncio
async def test_a_route_changed_after_the_start_keeps_newcomers_out(
    live_client: AsyncClient,
) -> None:
    group, _url, _anna, (_vera, vera_h), _started = await _started_pair(live_client)
    original = await _scalar(
        "SELECT content_updated_at FROM routes WHERE id = :id", id=group["route_id"]
    )
    try:
        await _sql(
            "UPDATE routes SET content_updated_at = now() WHERE id = :id", id=group["route_id"]
        )
        run = await live_client.post(
            RUNS,
            headers=vera_h,
            json={"route_id": group["route_id"], "companion_group_id": group["id"]},
        )
        assert run.status_code == 409
        assert run.json()["error"]["code"] == "companion_route_changed"
    finally:
        await _sql(
            "UPDATE routes SET content_updated_at = :value WHERE id = :id",
            id=group["route_id"],
            value=original,
        )


async def _scalar(statement: str, **params: object) -> object:
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        value = await conn.scalar(text(statement), params)
    await engine.dispose()
    return value
