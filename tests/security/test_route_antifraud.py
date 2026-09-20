"""Anti-fraud for route completion, end to end against Postgres and Redis.

Marks are spaced with the offline ``occurred_at`` field after moving the run's
start into the past, so the tests exercise the same paths a real offline sync
takes without waiting for wall-clock time.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.route_execution.application import antifraud_actions
from tourism_backend.modules.route_execution.application.antifraud_retention import (
    eligible_violation_ids,
    retention_cutoff,
)
from tourism_backend.modules.route_execution.infrastructure.models import RoutePointsHold

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")
# Three stops 8.8 km and 4.4 km apart (seeded by scripts/seed_crimea.py), so both legs
# have estimates well above the 120 s minimum the pace rule needs.
ROUTE_NAME = "Классика Южного берега"


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


@pytest.fixture
async def db() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM runtime_settings WHERE key LIKE 'af_%'"))
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM runtime_settings WHERE key LIKE 'af_%'"))
    await engine.dispose()


async def _set(engine: AsyncEngine, **values: str) -> None:
    async with engine.begin() as conn:
        for key, value in values.items():
            await conn.execute(
                text(
                    "INSERT INTO runtime_settings (key, value, updated_at) "
                    "VALUES (:key, :value, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = :value"
                ),
                {"key": key, "value": value},
            )


async def _scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar()


async def _all(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return [row[0] for row in (await conn.execute(text(sql), params)).all()]


async def _login(client: AsyncClient) -> tuple[dict[str, str], UUID]:
    phone = f"+7900{uuid4().int % 10_000_000:07d}"
    requested = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Испытатель антифрода", "phone": phone},
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
    body = verified.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = await client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, UUID(me.json()["id"])


async def _route_id(engine: AsyncEngine) -> str:
    value = await _scalar(engine, "SELECT id FROM routes WHERE name = :name", name=ROUTE_NAME)
    assert value is not None, "seed the Crimea data first (scripts/seed_crimea.py)"
    return str(value)


async def _start(
    client: AsyncClient,
    engine: AsyncEngine,
    headers: dict[str, str],
    route_id: str,
) -> dict[str, Any]:
    started = await client.post(
        "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
    )
    assert started.status_code == 201, started.text
    execution: dict[str, Any] = started.json()
    # Move the start into the past so spaced offline timestamps stay inside the run.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE route_executions SET started_at = now() - interval '5 hours' WHERE id = :id"
            ),
            {"id": execution["id"]},
        )
    return execution


async def _mark(
    client: AsyncClient,
    headers: dict[str, str],
    execution_id: str,
    stop_id: str,
    *,
    at: datetime,
    position: dict[str, float] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "client_event_id": str(uuid4()),
        "occurred_at": at.isoformat(),
    }
    if position is not None:
        payload["position"] = position
    response = await client.put(
        f"/api/v1/route-executions/{execution_id}/stops/{stop_id}/complete",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _fast_run(
    client: AsyncClient,
    engine: AsyncEngine,
    headers: dict[str, str],
    route_id: str,
    *,
    first_mark_minutes_ago: int,
    spacing_seconds: int = 90,
    complete: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Mark every stop in order, ``spacing_seconds`` apart (far under the estimates)."""

    execution = await _start(client, engine, headers, route_id)
    base = datetime.now(UTC) - timedelta(minutes=first_mark_minutes_ago)
    marks: list[dict[str, Any]] = []
    for index, stop in enumerate(execution["stops"]):
        marks.append(
            await _mark(
                client,
                headers,
                execution["id"],
                stop["id"],
                at=base + timedelta(seconds=index * spacing_seconds),
            )
        )
    if complete:
        finished = await client.post(
            f"/api/v1/route-executions/{execution['id']}/complete",
            headers=headers,
        )
        assert finished.status_code == 200, finished.text
        marks.append(finished.json())
    return execution, marks


async def _honest_run(
    client: AsyncClient,
    engine: AsyncEngine,
    headers: dict[str, str],
    route_id: str,
) -> dict[str, Any]:
    """Mark stops as slowly as the estimates say (no violation, no floor hit)."""

    execution = await _start(client, engine, headers, route_id)
    legs = [s["leg_estimate_seconds"] or 0 for s in execution["stops"]]
    spacing = int(max(legs) * 1.2) + 60
    base = datetime.now(UTC) - timedelta(seconds=spacing * len(legs) + 120)
    for index, stop in enumerate(execution["stops"]):
        await _mark(
            client,
            headers,
            execution["id"],
            stop["id"],
            at=base + timedelta(seconds=index * spacing),
        )
    finished = await client.post(
        f"/api/v1/route-executions/{execution['id']}/complete",
        headers=headers,
    )
    assert finished.status_code == 200, finished.text
    body: dict[str, Any] = finished.json()
    return body


# --------------------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_run_start_exposes_leg_estimates_but_no_hints_in_shadow(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    headers, _user = await _login(live_client)
    execution = await _start(live_client, db, headers, await _route_id(db))

    stops = execution["stops"]
    assert stops[0]["leg_estimate_seconds"] is None
    assert all(stop["leg_estimate_seconds"] and stop["leg_distance_meters"] for stop in stops[1:])
    assert all(stop["leg_estimate_source"] == "straight_line" for stop in stops[1:])
    # Shadow (the default): no thresholds and no antifraud block for the client.
    assert execution["antifraud"] is None
    assert all(stop["pace_warn_below_seconds"] is None for stop in stops)


@pytest.mark.asyncio
async def test_enforce_exposes_client_hints_after_the_first_mark(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    await _set(db, af_mode="enforce")
    headers, _user = await _login(live_client)
    execution = await _start(live_client, db, headers, await _route_id(db))

    assert execution["antifraud"] == {
        "mode": "enforce",
        "gps_tolerance_m": 150,
        "gps_min_accuracy_m": 100,
        "route_cooldown_days": 14,
        "daily_points_cap": 600,
    }
    # The first mark is never evaluated, so nothing to warn about yet.
    assert all(stop["pace_warn_below_seconds"] is None for stop in execution["stops"])

    marked = await _mark(
        live_client,
        headers,
        execution["id"],
        execution["stops"][0]["id"],
        at=datetime.now(UTC) - timedelta(hours=2),
    )
    later = marked["stops"][1]
    assert later["pace_warn_below_seconds"] == -(-later["leg_estimate_seconds"] // 2)
    assert marked["stops"][0]["pace_warn_below_seconds"] is None  # already marked


@pytest.mark.asyncio
async def test_shadow_records_violations_but_punishes_nobody(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    headers, user_id = await _login(live_client)
    _execution, marks = await _fast_run(
        live_client, db, headers, await _route_id(db), first_mark_minutes_ago=100
    )
    finished = marks[-1]

    assert finished["points_status"] == "awarded"
    assert finished["awarded_points"] > 0
    assert finished["held_points"] == 0
    assert (
        await _scalar(
            db,
            "SELECT count(*) FROM route_pace_violations WHERE user_id = :u AND mode = 'shadow'",
            u=user_id,
        )
        >= 1
    )
    assert (
        await _scalar(db, "SELECT count(*) FROM user_fraud_state WHERE user_id = :u", u=user_id)
        == 0
    )
    assert (
        await _scalar(
            db,
            "SELECT count(*) FROM notifications WHERE user_id = :u AND kind LIKE 'antifraud_%'",
            u=user_id,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_flag_then_block_hold_points_and_refuse_new_runs(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    await _set(db, af_mode="enforce", af_flag_violations="2", af_block_violations="3")
    headers, user_id = await _login(live_client)
    route_id = await _route_id(db)

    # Run 1: two counted violations (marks 2-3, spaced over the batch window)
    # reach the flag threshold; the run's points are then held, not credited.
    _first, marks = await _fast_run(live_client, db, headers, route_id, first_mark_minutes_ago=150)
    first_done = marks[-1]
    assert first_done["points_status"] == "held"
    assert first_done["awarded_points"] == 0
    assert first_done["held_points"] > 0
    assert (
        await _scalar(db, "SELECT is_flagged FROM user_fraud_state WHERE user_id = :u", u=user_id)
        is True
    )
    assert (
        await _scalar(
            db,
            "SELECT count(*) FROM route_points_holds "
            "WHERE user_id = :u AND reason = 'flag_forward'",
            u=user_id,
        )
        == 1
    )

    # Run 2: the third counted violation inside six hours -> block.
    _second, marks_2 = await _fast_run(
        live_client, db, headers, route_id, first_mark_minutes_ago=60
    )
    # Blocking never interrupts a run already in progress: it still completes.
    assert marks_2[-1]["status"] == "completed"
    blocked_until = await _scalar(
        db, "SELECT blocked_until FROM user_fraud_state WHERE user_id = :u", u=user_id
    )
    assert blocked_until is not None
    assert blocked_until > datetime.now(UTC)

    refused = await live_client.post(
        "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
    )
    assert refused.status_code == 403, refused.text
    error = refused.json()["error"]
    assert error["code"] == "route_start_blocked"
    assert error["details"]["blocked_until"]

    kinds = await _scalar(
        db,
        "SELECT string_agg(kind, ',' ORDER BY kind) FROM notifications "
        "WHERE user_id = :u AND kind LIKE 'antifraud_%'",
        u=user_id,
    )
    assert "antifraud_flagged" in kinds
    assert "antifraud_blocked" in kinds
    # The inbox must serialize the new kinds instead of failing.
    inbox = await live_client.get("/api/v1/me/notifications", headers=headers)
    assert inbox.status_code == 200, inbox.text
    assert {item["kind"] for item in inbox.json()["items"]} >= {
        "antifraud_flagged",
        "antifraud_blocked",
    }

    # An operator lifts the block: a new run starts again.
    maker = async_sessionmaker(db, expire_on_commit=False)
    async with maker() as session:
        await antifraud_actions.lift_block(session, user_id=user_id)
    restarted = await live_client.post(
        "/api/v1/route-executions", json={"route_id": route_id}, headers=headers
    )
    assert restarted.status_code == 201, restarted.text


@pytest.mark.asyncio
async def test_hold_decisions_are_idempotent_and_move_the_balance(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    await _set(db, af_mode="enforce", af_flag_violations="2")
    headers, user_id = await _login(live_client)
    route_id = await _route_id(db)
    _run, marks = await _fast_run(live_client, db, headers, route_id, first_mark_minutes_ago=150)
    held = marks[-1]["held_points"]
    assert held > 0

    hold_id = await _scalar(db, "SELECT id FROM route_points_holds WHERE user_id = :u", u=user_id)
    assert await _scalar(db, "SELECT travel_points FROM users WHERE id = :u", u=user_id) == 0

    maker = async_sessionmaker(db, expire_on_commit=False)
    async with maker() as session:
        first = await antifraud_actions.decide_hold(
            session, hold_id=hold_id, approve=True, principal_id=None, note="проверено"
        )
        assert first.status == "approved"
    # A second click (or a rejection racing the approval) changes nothing.
    async with maker() as session:
        again = await antifraud_actions.decide_hold(
            session, hold_id=hold_id, approve=False, principal_id=None
        )
        assert again.status == "approved"

    assert await _scalar(db, "SELECT travel_points FROM users WHERE id = :u", u=user_id) == held
    assert (
        await _scalar(
            db, "SELECT points_status FROM route_executions WHERE user_id = :u", u=user_id
        )
        == "awarded"
    )
    assert (
        await _scalar(
            db,
            "SELECT count(*) FROM notifications WHERE user_id = :u "
            "AND kind = 'antifraud_points_decision'",
            u=user_id,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_retro_hold_takes_credited_points_back_when_the_flag_is_raised(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    await _set(db, af_mode="enforce", af_flag_violations="2")
    headers, user_id = await _login(live_client)
    route_id = await _route_id(db)

    # One counted violation (mark 2) is below the flag threshold; mark 3 is slow
    # enough to be normal, then the run completes and credits its points.
    execution = await _start(live_client, db, headers, route_id)
    base = datetime.now(UTC) - timedelta(minutes=100)
    for index, stop in enumerate(execution["stops"][:2]):
        await _mark(
            live_client,
            headers,
            execution["id"],
            stop["id"],
            at=base + timedelta(seconds=index * 90),
        )
    await _mark(
        live_client,
        headers,
        execution["id"],
        execution["stops"][2]["id"],
        at=base + timedelta(hours=1),  # slow enough to be a normal mark
    )
    finished = await live_client.post(
        f"/api/v1/route-executions/{execution['id']}/complete", headers=headers
    )
    assert finished.json()["points_status"] == "awarded"
    credited = finished.json()["awarded_points"]
    assert credited > 0
    assert await _scalar(db, "SELECT travel_points FROM users WHERE id = :u", u=user_id) == credited

    # A second run adds a second counted violation inside the window: the flag
    # goes up and the FIRST run's credited points are pulled back into a hold.
    second = await _start(live_client, db, headers, route_id)
    base_2 = datetime.now(UTC) - timedelta(minutes=30)
    await _mark(live_client, headers, second["id"], second["stops"][0]["id"], at=base_2)
    await _mark(
        live_client,
        headers,
        second["id"],
        second["stops"][1]["id"],
        at=base_2 + timedelta(seconds=90),
    )

    assert (
        await _scalar(db, "SELECT is_flagged FROM user_fraud_state WHERE user_id = :u", u=user_id)
        is True
    )
    retro = await _scalar(
        db,
        "SELECT count(*) FROM route_points_holds WHERE user_id = :u AND reason = 'flag_retro'",
        u=user_id,
    )
    assert retro == 1
    assert await _scalar(db, "SELECT travel_points FROM users WHERE id = :u", u=user_id) == 0
    assert (
        await _scalar(
            db,
            "SELECT points_status FROM route_executions WHERE id = :id",
            id=UUID(execution["id"]),
        )
        == "held"
    )


@pytest.mark.asyncio
async def test_route_cooldown_pays_a_repeat_nothing_and_says_why(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    await _set(db, af_mode="enforce")
    headers, user_id = await _login(live_client)
    route_id = await _route_id(db)

    first = await _honest_run(live_client, db, headers, route_id)
    assert first["points_status"] == "awarded"
    assert first["awarded_points"] > 0
    assert first["points_reason"] is None

    repeat = await _honest_run(live_client, db, headers, route_id)
    assert repeat["awarded_points"] == 0
    assert repeat["points_status"] == "none"
    assert repeat["points_reason"] == "route_cooldown"
    assert (
        await _scalar(db, "SELECT travel_points FROM users WHERE id = :u", u=user_id)
        == first["awarded_points"]
    )


@pytest.mark.asyncio
async def test_daily_cap_pays_only_the_remainder(live_client: AsyncClient, db: AsyncEngine) -> None:
    await _set(db, af_mode="enforce", af_daily_points_cap="100")
    headers, user_id = await _login(live_client)
    # Earlier today this user already earned 95 points on some other run.
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO route_executions (id, user_id, route_name, status, started_at, "
                "completed_at, awarded_points, computed_points, points_status, "
                "created_at, updated_at) "
                "VALUES (:id, :u, 'Ранее сегодня', 'completed', now() - interval '2 minutes', "
                "now() - interval '1 minute', 95, 95, 'awarded', now(), now())"
            ),
            {"id": uuid4(), "u": user_id},
        )

    finished = await _honest_run(live_client, db, headers, await _route_id(db))
    assert finished["points_reason"] == "daily_cap"
    assert finished["awarded_points"] == 5
    assert finished["points_status"] == "awarded"


@pytest.mark.asyncio
async def test_gps_ahead_is_a_violation_and_being_on_site_is_not(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    await _set(db, af_mode="enforce")
    headers, user_id = await _login(live_client)
    execution = await _start(live_client, db, headers, await _route_id(db))
    stops = execution["stops"]

    def at(stop: dict[str, Any]) -> dict[str, float]:
        return {"lat": stop["lat"], "lng": stop["lng"], "accuracy_m": 10.0}

    now = datetime.now(UTC)
    first = await _mark(
        live_client,
        headers,
        execution["id"],
        stops[0]["id"],
        at=now - timedelta(hours=2),
        position=at(stops[0]),
    )
    assert first["pace_verdict"] == "ok"

    # Standing at stop 1 but marking stop 3: ahead of where the user is.
    ahead = await _mark(
        live_client,
        headers,
        execution["id"],
        stops[2]["id"],
        at=now - timedelta(hours=1, minutes=30),
        position=at(stops[0]),
    )
    assert ahead["pace_verdict"] == "ahead"

    # Standing at stop 3 and marking stop 2: the user is beyond it, which is normal.
    behind = await _mark(
        live_client,
        headers,
        execution["id"],
        stops[1]["id"],
        at=now - timedelta(hours=1),
        position=at(stops[2]),
    )
    assert behind["pace_verdict"] == "ok"

    async with db.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT kind, gps_verdict, gps_distance_bucket_m FROM route_pace_violations "
                    "WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).all()
    assert [(row[0], row[1]) for row in rows] == [("ahead", "ahead")]
    assert rows[0][2] is not None
    assert rows[0][2] % 50 == 0  # rounded distance, never raw coordinates


@pytest.mark.asyncio
async def test_a_trusted_user_is_never_flagged(live_client: AsyncClient, db: AsyncEngine) -> None:
    await _set(db, af_mode="enforce")
    headers, user_id = await _login(live_client)
    maker = async_sessionmaker(db, expire_on_commit=False)
    async with maker() as session:
        await antifraud_actions.set_trusted(session, user_id=user_id, trusted=True)

    _run, marks = await _fast_run(
        live_client, db, headers, await _route_id(db), first_mark_minutes_ago=100
    )
    assert marks[-1]["points_status"] == "awarded"
    assert (
        await _scalar(
            db, "SELECT count(*) FROM route_pace_violations WHERE user_id = :u", u=user_id
        )
        == 0
    )
    assert (
        await _scalar(db, "SELECT is_flagged FROM user_fraud_state WHERE user_id = :u", u=user_id)
        is False
    )


@pytest.mark.asyncio
async def test_retention_keeps_events_of_open_or_recently_closed_holds(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    await _set(db, af_mode="enforce", af_flag_violations="2", af_block_violations="6")
    headers, user_id = await _login(live_client)
    route_id = await _route_id(db)
    execution, marks = await _fast_run(
        live_client, db, headers, route_id, first_mark_minutes_ago=150
    )
    assert marks[-1]["points_status"] == "held"

    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE route_pace_violations SET occurred_at = now() - interval '200 days' "
                "WHERE user_id = :u"
            ),
            {"u": user_id},
        )
    cutoff = retention_cutoff(90)
    mine = set(await _all(db, "SELECT id FROM route_pace_violations WHERE user_id = :u", u=user_id))
    assert mine

    async def purgeable(session: Any) -> set[UUID]:
        found = await session.scalars(eligible_violation_ids(cutoff=cutoff, limit=100_000))
        return set(found) & mine

    async with async_sessionmaker(db)() as session:
        # Hold still open: nothing may go.
        assert await purgeable(session) == set()

    maker = async_sessionmaker(db, expire_on_commit=False)
    async with maker() as session:
        hold_id = await session.scalar(
            select(RoutePointsHold.id).where(RoutePointsHold.execution_id == UUID(execution["id"]))
        )
        assert hold_id is not None
        await antifraud_actions.decide_hold(
            session, hold_id=hold_id, approve=False, principal_id=None
        )
    async with maker() as session:
        # Closed just now: still inside the window.
        assert await purgeable(session) == set()
    async with db.begin() as conn:
        await conn.execute(
            text(
                "UPDATE route_points_holds SET decided_at = now() - interval '100 days' "
                "WHERE execution_id = :e"
            ),
            {"e": execution["id"]},
        )
    async with maker() as session:
        assert await purgeable(session) == mine
