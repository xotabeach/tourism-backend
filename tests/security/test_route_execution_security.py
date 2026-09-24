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
    badges = await live_client.get("/api/v1/me/achievements", headers=headers)
    first_step = [item for item in badges.json()["items"] if item["slug"] == "first-step"]
    assert len(first_step) == 1
    assert first_step[0]["status"] == "unlocked"
    inbox = await live_client.get("/api/v1/me/notifications", headers=headers)
    notices = [item for item in inbox.json()["items"] if item["target_id"] == first_step[0]["id"]]
    assert len(notices) == 1
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
async def test_route_execution_pause_resume(live_client: AsyncClient) -> None:
    tokens = await _login(live_client, f"+7903{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    route_id, _ = await _catalog_route(live_client)

    started = await live_client.post(
        "/api/v1/route-executions",
        json={"route_id": route_id},
        headers=headers,
    )
    assert started.status_code == 201, started.text
    execution_id = started.json()["id"]

    paused = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/pause",
        headers=headers,
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"

    # A paused run must not vanish from /active — otherwise reopening the
    # app after a pause would look like there's nothing in progress, and
    # starting anything would silently orphan this run instead of erroring.
    still_active = await live_client.get(
        "/api/v1/route-executions/active",
        headers=headers,
    )
    assert still_active.status_code == 200
    assert still_active.json() is not None
    assert still_active.json()["id"] == execution_id
    assert still_active.json()["status"] == "paused"

    # A paused run still blocks starting a different route — it's the run
    # you're on either way.
    routes = await live_client.get("/api/v1/routes", params={"limit": 2})
    other_route_id = next(
        (item["id"] for item in routes.json()["items"] if item["id"] != route_id),
        None,
    )
    if other_route_id is not None:
        blocked_start = await live_client.post(
            "/api/v1/route-executions",
            json={"route_id": other_route_id},
            headers=headers,
        )
        assert blocked_start.status_code == 409
        assert blocked_start.json()["error"]["code"] == "active_route_execution_exists"

    # Idempotent: pausing an already-paused run just returns current state.
    paused_again = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/pause",
        headers=headers,
    )
    assert paused_again.status_code == 200
    assert paused_again.json()["status"] == "paused"

    # No progress is possible while paused — resume first.
    blocked_stop = await live_client.put(
        f"/api/v1/route-executions/{execution_id}"
        f"/stops/{started.json()['stops'][0]['id']}/complete",
        headers=headers,
    )
    assert blocked_stop.status_code == 409
    assert blocked_stop.json()["error"]["code"] == "route_execution_not_active"

    blocked_finish = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/complete",
        headers=headers,
    )
    assert blocked_finish.status_code == 409

    resumed = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/resume",
        headers=headers,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"
    assert resumed.json()["paused_duration_seconds"] >= 0

    # Idempotent: resuming an already-active run just returns current state.
    resumed_again = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/resume",
        headers=headers,
    )
    assert resumed_again.status_code == 200
    assert resumed_again.json()["status"] == "active"

    for stop in started.json()["stops"]:
        completed = await live_client.put(
            f"/api/v1/route-executions/{execution_id}/stops/{stop['id']}/complete",
            headers=headers,
        )
        assert completed.status_code == 200, completed.text

    finished = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/complete",
        headers=headers,
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_route_execution_pause_is_owner_scoped_and_cancel_works_while_paused(
    live_client: AsyncClient,
) -> None:
    owner_tokens = await _login(live_client, f"+7904{uuid4().int % 10_000_000:07d}")
    stranger_tokens = await _login(live_client, f"+7905{uuid4().int % 10_000_000:07d}")
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

    forbidden_pause = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/pause",
        headers=stranger_headers,
    )
    assert forbidden_pause.status_code == 404

    paused = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/pause",
        headers=owner_headers,
    )
    assert paused.status_code == 200, paused.text

    forbidden_resume = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/resume",
        headers=stranger_headers,
    )
    assert forbidden_resume.status_code == 404

    # Abandoning a paused run must not require resuming first.
    cancelled = await live_client.post(
        f"/api/v1/route-executions/{execution_id}/cancel",
        headers=owner_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


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


@pytest.mark.asyncio
async def test_achievement_failure_does_not_rollback_completion(
    live_client: AsyncClient, monkeypatch
):
    from tourism_backend.modules.achievements import service as awards

    async def broken(*args, **kwargs):
        raise RuntimeError("simulated achievement query failure")

    monkeypatch.setattr(awards, "evaluate", broken)
    tokens = await _login(live_client, f"+7922{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    route_id, _ = await _catalog_route(live_client)
    response = await live_client.post(
        "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
    )
    assert response.status_code == 201, response.text
    run = response.json()
    for stop in run["stops"]:
        marked = await live_client.put(
            f"/api/v1/route-executions/{run['id']}/stops/{stop['id']}/complete", headers=headers
        )
        assert marked.status_code == 200, marked.text
    finished = await live_client.post(
        f"/api/v1/route-executions/{run['id']}/complete", headers=headers
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "completed"
    # The committed result remains readable even though every award attempt failed.
    history = await live_client.get("/api/v1/route-executions", headers=headers)
    assert any(
        item["id"] == run["id"] and item["status"] == "completed"
        for item in history.json()["items"]
    )


@pytest.mark.asyncio
async def test_only_the_latest_mark_can_be_taken_back(live_client: AsyncClient) -> None:
    """FRONTEND-36: unmark the last marked stop while the run is in progress."""
    tokens = await _login(live_client, f"+7914{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    listed = await live_client.get("/api/v1/routes", params={"limit": 20})
    route_id = next(item["id"] for item in listed.json()["items"] if item["stops_count"] >= 2)

    started = await live_client.post(
        "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
    )
    assert started.status_code == 201, started.text
    execution_id = started.json()["id"]
    first, second = started.json()["stops"][:2]
    base = f"/api/v1/route-executions/{execution_id}/stops"
    try:
        for stop in (first, second):
            marked = await live_client.put(f"{base}/{stop['id']}/complete", headers=headers)
            assert marked.status_code == 200, marked.text

        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        count_sql = text("SELECT count(*) FROM route_pace_violations WHERE execution_id = :id")
        async with engine.connect() as conn:
            violations_before = await conn.scalar(count_sql, {"id": execution_id})

        # Not the latest mark: refused.
        earlier = await live_client.delete(f"{base}/{first['id']}/complete", headers=headers)
        assert earlier.status_code == 409, earlier.text
        assert earlier.json()["error"]["code"] == "route_execution_stop_not_last"

        event = {"client_event_id": str(uuid4())}
        undone = await live_client.request(
            "DELETE", f"{base}/{second['id']}/complete", json=event, headers=headers
        )
        assert undone.status_code == 200, undone.text
        body = undone.json()
        assert body["completed_stops"] == 1
        assert next(s for s in body["stops"] if s["id"] == second["id"])["completed_at"] is None

        # A retried request with the same event, or a second unmark, changes nothing.
        replay = await live_client.request(
            "DELETE", f"{base}/{second['id']}/complete", json=event, headers=headers
        )
        assert replay.status_code == 200
        assert replay.json()["completed_stops"] == 1
        again = await live_client.delete(f"{base}/{second['id']}/complete", headers=headers)
        assert again.status_code == 200
        assert again.json()["completed_stops"] == 1

        # The first stop is now the latest mark and can be taken back too.
        first_undone = await live_client.delete(f"{base}/{first['id']}/complete", headers=headers)
        assert first_undone.status_code == 200, first_undone.text
        assert first_undone.json()["completed_stops"] == 0

        async with engine.connect() as conn:
            assert await conn.scalar(count_sql, {"id": execution_id}) == violations_before
        await engine.dispose()

        # A finished run cannot be unmarked.
        remarked = await live_client.put(f"{base}/{first['id']}/complete", headers=headers)
        assert remarked.status_code == 200
        cancelled = await live_client.post(
            f"/api/v1/route-executions/{execution_id}/cancel", headers=headers
        )
        assert cancelled.status_code == 200, cancelled.text
        late = await live_client.delete(f"{base}/{first['id']}/complete", headers=headers)
        assert late.status_code == 409
        assert late.json()["error"]["code"] == "route_execution_not_active"
    finally:
        await live_client.post(f"/api/v1/route-executions/{execution_id}/cancel", headers=headers)


@pytest.mark.asyncio
async def test_run_reports_pause_start_last_activity_and_own_review(
    live_client: AsyncClient,
) -> None:
    """FRONTEND-34: what the home card needs to draw a run."""
    tokens = await _login(live_client, f"+7915{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = (await live_client.get("/api/v1/me", headers=headers)).json()
    listed = await live_client.get("/api/v1/routes", params={"limit": 20})
    route_id = next(item["id"] for item in listed.json()["items"] if item["stops_count"] >= 1)
    started = await live_client.post(
        "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
    )
    assert started.status_code == 201, started.text
    run = started.json()
    base = f"/api/v1/route-executions/{run['id']}"
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        assert run["paused_at"] is None
        assert run["last_activity_at"] == run["started_at"]
        assert run["my_review_exists"] is False

        paused = (await live_client.post(f"{base}/pause", headers=headers)).json()
        assert paused["paused_at"] is not None
        # A pause is not activity: «давно не отмечали» counts from the start.
        assert paused["last_activity_at"] == run["started_at"]

        resumed = (await live_client.post(f"{base}/resume", headers=headers)).json()
        assert resumed["paused_at"] is None
        assert resumed["last_activity_at"] > run["started_at"]

        for stop in run["stops"]:
            marked = await live_client.put(f"{base}/stops/{stop['id']}/complete", headers=headers)
            assert marked.status_code == 200, marked.text
        last_mark = max(stop["completed_at"] for stop in marked.json()["stops"])
        assert marked.json()["last_activity_at"] == last_mark

        done = await live_client.post(f"{base}/complete", headers=headers)
        assert done.status_code == 200, done.text
        assert done.json()["my_review_exists"] is False

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO route_reviews (id, route_id, author_user_id, body, rating,"
                    " status, created_at, updated_at) VALUES (:id, :route, :user, 'ok', 5,"
                    " 'pending_review', now(), now())"
                ),
                {"id": str(uuid4()), "route": route_id, "user": me["id"]},
            )
        again = await live_client.get(base, headers=headers)
        assert again.json()["my_review_exists"] is True
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM route_reviews WHERE author_user_id = :user"),
                {"user": me["id"]},
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_parallel_starts_leave_one_run_and_name_the_blocking_one(
    live_client: AsyncClient,
) -> None:
    """BACKEND-25: double taps, two devices and restarts after a cancel."""
    import asyncio

    tokens = await _login(live_client, f"+7901{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    routes = await live_client.get("/api/v1/routes", params={"limit": 2})
    items = routes.json()["items"]
    assert len(items) >= 2, "seeded catalog must contain two routes"
    first, second = items[0], items[1]

    async def start(route_id: str):
        return await live_client.post(
            "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
        )

    # The same route started five times at once is one run.
    same = await asyncio.gather(*(start(first["id"]) for _ in range(5)))
    assert all(r.status_code in (200, 201) for r in same), [r.text for r in same]
    assert len({r.json()["id"] for r in same}) == 1
    run_id = same[0].json()["id"]

    # Two devices racing different routes: never a second run, and the refusal
    # names the run in the way.
    raced = await asyncio.gather(start(second["id"]), start(second["id"]))
    for response in raced:
        assert response.status_code == 409, response.text
        error = response.json()["error"]
        assert error["code"] == "active_route_execution_exists"
        assert error["details"]["execution_id"] == run_id
        assert error["details"]["route_id"] == first["id"]
        assert error["details"]["route_name"] == same[0].json()["route_name"]

    # A cancelled run no longer blocks: the same route starts afresh.
    cancelled = await live_client.post(f"/api/v1/route-executions/{run_id}/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    again = await start(first["id"])
    assert again.status_code == 201, again.text
    assert again.json()["id"] != run_id


@pytest.mark.asyncio
async def test_run_start_keeps_the_route_days_and_segments_in_its_snapshot(
    live_client: AsyncClient,
) -> None:
    """Spec 14, step 0: one day, a segment per leg, frozen with the snapshot."""

    tokens = await _login(live_client, f"+7900{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    route_id, _ = await _catalog_route(live_client)
    started = await live_client.post(
        "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
    )
    assert started.status_code == 201, started.text
    execution = started.json()
    snapshot_id = execution["routing"]["snapshot_id"]

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            stop_count = await conn.scalar(
                text("SELECT count(*) FROM route_stops WHERE route_id = :id"), {"id": route_id}
            )
            route_segments = await conn.scalar(
                text("SELECT count(*) FROM route_segments WHERE route_id = :id"),
                {"id": route_id},
            )
            route_days = (
                await conn.execute(
                    text(
                        "SELECT day_index, boundary_source FROM route_days "
                        "WHERE route_id = :id ORDER BY day_index"
                    ),
                    {"id": route_id},
                )
            ).all()
            snapshot_segments = (
                await conn.execute(
                    text(
                        "SELECT leg_index, mode, role FROM routing_snapshot_segments "
                        "WHERE snapshot_id = :id ORDER BY leg_index, seq"
                    ),
                    {"id": snapshot_id},
                )
            ).all()
            snapshot_days = (
                await conn.execute(
                    text(
                        "SELECT day_index, first_position, last_position "
                        "FROM routing_snapshot_days WHERE snapshot_id = :id"
                    ),
                    {"id": snapshot_id},
                )
            ).all()
            base_mode = await conn.scalar(
                text("SELECT base_mode FROM route_routing_snapshots WHERE id = :id"),
                {"id": snapshot_id},
            )
        assert route_segments == stop_count - 1
        # Days follow the route's norms (spec 14a): numbered 1..n, all automatic.
        assert [row.day_index for row in route_days] == list(range(1, len(route_days) + 1))
        assert {row.boundary_source for row in route_days} == {"auto"}
        assert [row.leg_index for row in snapshot_segments] == list(range(stop_count - 1))
        assert {row.role for row in snapshot_segments} <= {"main"}
        assert {row.mode for row in snapshot_segments} <= {"walk", "car"}
        assert len(snapshot_days) == len(route_days)
        ordered = sorted(snapshot_days, key=lambda day: day.day_index)
        assert ordered[0].first_position == 1
        for before, after in zip(ordered, ordered[1:], strict=False):
            assert after.first_position == before.last_position + 1
        assert base_mode in {"walk", "car", "mixed"}

        with pytest.raises(DBAPIError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE routing_snapshot_segments SET distance_meters = 1 "
                        "WHERE snapshot_id = :id"
                    ),
                    {"id": snapshot_id},
                )
    finally:
        await engine.dispose()
        await live_client.post(
            f"/api/v1/route-executions/{execution['id']}/cancel", headers=headers
        )


@pytest.mark.asyncio
async def test_route_detail_serves_segments_once(live_client: AsyncClient) -> None:
    """Spec 14b: segments in their own field, not again inside accessibility."""

    tokens = await _login(live_client, f"+7900{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    route_id, _ = await _catalog_route(live_client)
    started = await live_client.post(
        "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
    )
    assert started.status_code == 201, started.text
    try:
        detail = await live_client.get(f"/api/v1/routes/{route_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["base_mode"] in {"walk", "car", "mixed"}
        legs = {segment["leg_index"] for segment in body["segments"]}
        assert legs == set(range(len(body["stops"]) - 1))
        for segment in body["segments"]:
            assert segment["mode"] in {"walk", "car"}
            assert segment["role"] in {"main", "approach", "return"}
        routing = (body.get("accessibility") or {}).get("routing") or {}
        assert "segments" not in routing
    finally:
        await live_client.post(
            f"/api/v1/route-executions/{started.json()['id']}/cancel", headers=headers
        )
