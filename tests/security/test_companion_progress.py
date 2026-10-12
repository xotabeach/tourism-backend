"""Progress, result, «кто первым» and «Компания» (BACKEND-61, spec 11b)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime
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


async def _scalar(statement: str, **params: object) -> object:
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        value = await conn.scalar(text(statement), params)
    await engine.dispose()
    return value


async def _run(client: AsyncClient, headers: dict[str, str], group: dict) -> dict:
    run = await client.post(
        RUNS,
        headers=headers,
        json={"route_id": group["route_id"], "companion_group_id": group["id"]},
    )
    assert run.status_code == 201, run.text
    return run.json()


async def _mark(
    client: AsyncClient, headers: dict[str, str], run: dict, index: int, **body: object
) -> None:
    stop = run["stops"][index]
    marked = await client.put(
        f"{RUNS}/{run['id']}/stops/{stop['id']}/complete", headers=headers, json=body or None
    )
    assert marked.status_code == 200, marked.text


async def _finish(client: AsyncClient, headers: dict[str, str], run: dict) -> None:
    for index in range(len(run["stops"])):
        await _mark(client, headers, run, index)
    done = await client.post(f"{RUNS}/{run['id']}/complete", headers=headers)
    assert done.status_code == 200, done.text


def _progress_member(progress: dict, user_id: str) -> dict:
    return next(m for m in progress["members"] if m["user_id"] == user_id)


@pytest.mark.asyncio
async def test_progress_firsts_and_an_offline_mark(live_client: AsyncClient) -> None:
    group, url, (anna, anna_h), (vera, vera_h), started = await _started_pair(live_client)
    anna_run = await _run(live_client, anna_h, group)
    vera_run = await _run(live_client, vera_h, group)

    before = (await live_client.get(f"{url}/progress", headers=vera_h)).json()
    assert {m["status"] for m in before["members"]} == {"walking"}
    assert before["stops"][0]["first_user_id"] is None

    # Vera marks the first stop online; Anna's mark reaches the server late,
    # saying it was made earlier: it is «без сети» and takes no first place.
    await _mark(live_client, vera_h, vera_run, 0)
    made = datetime.fromisoformat(started["started_at"])
    await _mark(
        live_client,
        anna_h,
        anna_run,
        0,
        client_event_id=str(uuid4()),
        occurred_at=made.isoformat(),
    )
    await _sql(
        "UPDATE route_execution_events SET recorded_at = recorded_at + interval '10 minutes' "
        "WHERE execution_id = :id",
        id=anna_run["id"],
    )
    progress = (await live_client.get(f"{url}/progress", headers=anna_h)).json()
    assert progress["leaderboard_hidden"] is False
    assert progress["stops"][0]["first_user_id"] == vera
    assert _progress_member(progress, anna)["marks"][0]["offline"] is True
    assert _progress_member(progress, vera)["marks"][0]["offline"] is False
    assert _progress_member(progress, vera)["last_position"] == 1

    # Strangers see nothing.
    _gleb, gleb_h = await _user(live_client, "Глеб")
    assert (await live_client.get(f"{url}/progress", headers=gleb_h)).status_code == 404


@pytest.mark.asyncio
async def test_the_result_is_fixed_when_everyone_has_ended(live_client: AsyncClient) -> None:
    group, url, (anna, anna_h), (vera, vera_h), _started = await _started_pair(live_client)
    anna_run = await _run(live_client, anna_h, group)
    vera_run = await _run(live_client, vera_h, group)
    await _finish(live_client, vera_h, vera_run)

    inbox = await live_client.get("/api/v1/me/notifications", headers=anna_h)
    assert any(n["kind"] == "companion_member_finished" for n in inbox.json()["items"])
    mid = (await live_client.get(f"{url}/progress", headers=anna_h)).json()
    assert mid["summary"]["settled"] is False
    assert _progress_member(mid, vera)["status"] == "finished"

    await _finish(live_client, anna_h, anna_run)
    done = (await live_client.get(f"{url}/progress", headers=anna_h)).json()
    assert done["status"] == "settled"
    summary = done["summary"]
    assert summary["settled"] is True
    assert summary["started_count"] == 2
    assert summary["reached_count"] == 2
    assert summary["everyone_reached"] is True
    assert summary["first_finisher_user_id"] == vera
    assert summary["total_seconds"] is not None

    # Both walked the same stops within the hour: «Компания» for both.
    for headers in (anna_h, vera_h):
        badges = (await live_client.get("/api/v1/me/achievements", headers=headers)).json()
        company = next(b for b in badges["items"] if b["slug"] == "group")
        assert company["is_unlocked"] is True, company


@pytest.mark.asyncio
async def test_seven_days_settle_a_group_with_someone_still_out(live_client: AsyncClient) -> None:
    group, url, (anna, anna_h), (vera, vera_h), _started = await _started_pair(live_client)
    await _run(live_client, anna_h, group)
    await _sql(
        "UPDATE companion_groups SET started_at = started_at - interval '8 days' WHERE id = :id",
        id=group["id"],
    )
    progress = (await live_client.get(f"{url}/progress", headers=anna_h)).json()
    assert progress["status"] == "settled"
    assert _progress_member(progress, vera)["status"] == "not_finished"
    assert _progress_member(progress, anna)["status"] == "not_finished"
    assert vera_h


@pytest.mark.asyncio
async def test_no_table_on_a_mountain_route(live_client: AsyncClient) -> None:
    group, url, (_anna, anna_h), (_vera, vera_h), _started = await _started_pair(live_client)
    vera_run = await _run(live_client, vera_h, group)
    await _mark(live_client, vera_h, vera_run, 0)
    level = await _scalar(
        "SELECT difficulty_level FROM routes WHERE id = :id", id=group["route_id"]
    )
    try:
        await _sql("UPDATE routes SET difficulty_level = 5 WHERE id = :id", id=group["route_id"])
        progress = (await live_client.get(f"{url}/progress", headers=anna_h)).json()
        assert progress["leaderboard_hidden"] is True
        assert all(stop["first_user_id"] is None for stop in progress["stops"])
    finally:
        await _sql(
            "UPDATE routes SET difficulty_level = :level WHERE id = :id",
            id=group["route_id"],
            level=level,
        )


@pytest.mark.asyncio
async def test_leaving_on_the_way_tells_the_others(live_client: AsyncClient) -> None:
    group, url, (_anna, anna_h), (vera, vera_h), _started = await _started_pair(live_client)
    left = await live_client.delete(f"{url}/members/{vera}", headers=vera_h)
    assert left.status_code == 204, left.text
    inbox = await live_client.get("/api/v1/me/notifications", headers=anna_h)
    assert any(n["kind"] == "companion_member_left" for n in inbox.json()["items"])
