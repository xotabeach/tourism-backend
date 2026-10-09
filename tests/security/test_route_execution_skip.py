"""Spec 15 (D1, D4, D14, D15): skipping a stop of a run."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
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
API = "/api/v1/route-executions"


@pytest.fixture
async def live_client() -> AsyncIterator[AsyncClient]:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT skipped_at FROM route_execution_stops LIMIT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
    except Exception:  # noqa: BLE001
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await app.state.redis.aclose()
    await app.state.engine.dispose()


async def _headers(client: AsyncClient) -> dict[str, str]:
    phone = f"+7907{uuid4().int % 10_000_000:07d}"
    requested = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Пропуск точки", "phone": phone},
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
    return {"Authorization": f"Bearer {verified.json()['access_token']}"}


async def _route_with_stops(client: AsyncClient, minimum: int = 3) -> str:
    response = await client.get("/api/v1/routes", params={"limit": 50})
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        if item["stops_count"] >= minimum:
            return str(item["id"])
    pytest.skip(f"the seeded catalog has no route with {minimum} stops")


async def _start(client: AsyncClient, headers: dict[str, str], route_id: str) -> dict[str, Any]:
    started = await client.post(API, json={"route_id": route_id}, headers=headers)
    assert started.status_code == 201, started.text
    body: dict[str, Any] = started.json()
    return body


async def _mark(client: AsyncClient, headers: dict[str, str], run: str, stop: str) -> None:
    marked = await client.put(f"{API}/{run}/stops/{stop}/complete", headers=headers)
    assert marked.status_code == 200, marked.text


async def _walk(
    client: AsyncClient,
    route_id: str,
    *,
    skip_index: int | None = None,
    reason: str = "no_time",
) -> dict[str, Any]:
    """A run of the route by a new person, optionally skipping one stop."""
    headers = await _headers(client)
    run = await _start(client, headers, route_id)
    for index, stop in enumerate(run["stops"]):
        if index == skip_index:
            skipped = await client.put(
                f"{API}/{run['id']}/stops/{stop['id']}/skip",
                json={"reason": reason},
                headers=headers,
            )
            assert skipped.status_code == 200, skipped.text
        else:
            await _mark(client, headers, run["id"], stop["id"])
    finished = await client.post(f"{API}/{run['id']}/complete", headers=headers)
    assert finished.status_code == 200, finished.text
    body: dict[str, Any] = finished.json()
    return body


async def test_a_skipped_stop_lets_the_run_end_pays_less_and_does_not_count(
    live_client: AsyncClient,
) -> None:
    route_id = await _route_with_stops(live_client)

    whole = await _walk(live_client, route_id)
    missed = await _walk(live_client, route_id, skip_index=1, reason="no_time")
    closed = await _walk(live_client, route_id, skip_index=1, reason="closed")

    assert (whole["status"], whole["counted"], whole["skipped_required_stops"]) == (
        "completed",
        True,
        0,
    )
    assert (whole["completed_share_percent"], whole["counted_threshold_percent"]) == (100, 70)
    for run in (missed, closed):
        assert run["status"] == "completed"
        # One skipped stop counts or not by the share of marked required
        # stops against the 70% threshold (BACKEND-36).
        share = (run["required_stops"] - 1) * 100 // run["required_stops"]
        assert run["completed_share_percent"] == share
        assert run["counted"] is (share >= 70)
        assert run["skipped_required_stops"] == 1
        assert run["completed_required_stops"] == run["required_stops"] - 1
        assert 0 < run["awarded_points"] < whole["awarded_points"]
    # «Закрыто»: the way to the stop was walked and is paid, only the mark is not.
    assert closed["awarded_points"] >= missed["awarded_points"]
    # One skipped stop of several costs its own leg, not the whole way: the
    # run keeps more than the bare «started and marked» amount.
    stops_only = 10 + 3 * (missed["required_stops"] - 1)
    way_walked = sum(
        stop["leg_distance_meters"] or 0
        for stop in missed["stops"]
        if stop["completed_at"] is not None
    )
    if way_walked >= 10_000:
        assert missed["awarded_points"] > stops_only
    assert [stop["skip_reason"] for stop in closed["stops"]].count("closed") == 1
    skipped_stop = next(stop for stop in missed["stops"] if stop["skip_reason"])
    assert skipped_stop["skipped_at"] is not None
    assert skipped_stop["completed_at"] is None


async def test_skip_rules_of_a_run_in_progress(live_client: AsyncClient) -> None:
    route_id = await _route_with_stops(live_client)
    headers = await _headers(live_client)
    run = await _start(live_client, headers, route_id)
    run_id = run["id"]
    first, second, *rest = run["stops"]
    try:
        # A skip always carries one of the known reasons.
        for payload in ({}, {"reason": "bored"}):
            bad = await live_client.put(
                f"{API}/{run_id}/stops/{second['id']}/skip", json=payload, headers=headers
            )
            assert bad.status_code == 422, bad.text

        await _mark(live_client, headers, run_id, first["id"])
        marked_skip = await live_client.put(
            f"{API}/{run_id}/stops/{first['id']}/skip", json={"reason": "closed"}, headers=headers
        )
        assert marked_skip.status_code == 409
        assert marked_skip.json()["error"]["code"] == "route_execution_stop_already_marked"

        event_id = str(uuid4())
        skipped = await live_client.put(
            f"{API}/{run_id}/stops/{second['id']}/skip",
            json={"reason": "hard", "client_event_id": event_id},
            headers=headers,
        )
        assert skipped.status_code == 200, skipped.text
        assert skipped.json()["sync"]["action"] == "skip_stop"
        replay = await live_client.put(
            f"{API}/{run_id}/stops/{second['id']}/skip",
            json={"reason": "hard", "client_event_id": event_id},
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json()["sync"]["replayed"] is True

        # Another reason replaces the first; the moment of the skip stays.
        changed = await live_client.put(
            f"{API}/{run_id}/stops/{second['id']}/skip", json={"reason": "closed"}, headers=headers
        )
        stop_now = next(s for s in changed.json()["stops"] if s["id"] == second["id"])
        stop_before = next(s for s in skipped.json()["stops"] if s["id"] == second["id"])
        assert stop_now["skip_reason"] == "closed"
        assert stop_now["skipped_at"] == stop_before["skipped_at"]

        # Taking the skip back makes the stop block the end again.
        unskipped = await live_client.delete(
            f"{API}/{run_id}/stops/{second['id']}/skip", headers=headers
        )
        assert unskipped.status_code == 200, unskipped.text
        assert unskipped.json()["skipped_required_stops"] == 0
        again = await live_client.delete(
            f"{API}/{run_id}/stops/{second['id']}/skip", headers=headers
        )
        assert again.status_code == 200
        for stop in rest:
            await _mark(live_client, headers, run_id, stop["id"])
        blocked = await live_client.post(f"{API}/{run_id}/complete", headers=headers)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "required_stops_incomplete"

        # Reaching a skipped stop after all: the mark replaces the skip.
        await live_client.put(
            f"{API}/{run_id}/stops/{second['id']}/skip", json={"reason": "no_time"}, headers=headers
        )
        await _mark(live_client, headers, run_id, second["id"])
        finished = await live_client.post(f"{API}/{run_id}/complete", headers=headers)
        assert finished.status_code == 200, finished.text
        body = finished.json()
        assert (body["counted"], body["skipped_required_stops"]) == (True, 0)
        assert all(stop["skip_reason"] is None for stop in body["stops"])
    finally:
        await live_client.post(f"{API}/{run_id}/cancel", headers=headers)


async def test_a_run_with_every_stop_skipped_cannot_be_completed(
    live_client: AsyncClient,
) -> None:
    route_id = await _route_with_stops(live_client)
    headers = await _headers(live_client)
    run = await _start(live_client, headers, route_id)
    try:
        for stop in run["stops"]:
            skipped = await live_client.put(
                f"{API}/{run['id']}/stops/{stop['id']}/skip",
                json={"reason": "other"},
                headers=headers,
            )
            assert skipped.status_code == 200, skipped.text
        finished = await live_client.post(f"{API}/{run['id']}/complete", headers=headers)
        assert finished.status_code == 409
        assert finished.json()["error"]["code"] == "no_stops_marked"
    finally:
        cancelled = await live_client.post(f"{API}/{run['id']}/cancel", headers=headers)
        assert cancelled.json()["counted"] is False
        assert cancelled.json()["completed_share_percent"] is None


async def test_an_early_end_pays_for_what_was_walked_and_does_not_count(
    live_client: AsyncClient,
) -> None:
    route_id = await _route_with_stops(live_client)
    whole = await _walk(live_client, route_id)

    headers = await _headers(live_client)
    run = await _start(live_client, headers, route_id)
    await _mark(live_client, headers, run["id"], run["stops"][0]["id"])
    await _mark(live_client, headers, run["id"], run["stops"][1]["id"])
    ended = await live_client.post(f"{API}/{run['id']}/finish-early", headers=headers)

    assert ended.status_code == 200, ended.text
    body = ended.json()
    assert (body["status"], body["ended_early"], body["counted"]) == ("cancelled", True, False)
    # Two of the required stops marked: the share is kept for the review mark.
    assert body["completed_share_percent"] == 2 * 100 // body["required_stops"]
    assert 0 < body["awarded_points"] < whole["awarded_points"]


async def test_skipping_is_owner_scoped_and_needs_a_login(live_client: AsyncClient) -> None:
    route_id = await _route_with_stops(live_client)
    owner = await _headers(live_client)
    stranger = await _headers(live_client)
    run = await _start(live_client, owner, route_id)
    url = f"{API}/{run['id']}/stops/{run['stops'][0]['id']}/skip"
    try:
        assert (await live_client.put(url, json={"reason": "closed"})).status_code == 401
        foreign = await live_client.put(url, json={"reason": "closed"}, headers=stranger)
        assert foreign.status_code == 404
        assert (await live_client.delete(url, headers=stranger)).status_code == 404
    finally:
        await live_client.post(f"{API}/{run['id']}/cancel", headers=owner)


async def _walk_skipping(
    client: AsyncClient, route_id: str, skip: set[int]
) -> tuple[dict[str, Any], dict[str, str]]:
    """A run by a new person that skips the stops at these indexes."""
    headers = await _headers(client)
    run = await _start(client, headers, route_id)
    for index, stop in enumerate(run["stops"]):
        if index in skip:
            skipped = await client.put(
                f"{API}/{run['id']}/stops/{stop['id']}/skip",
                json={"reason": "no_time"},
                headers=headers,
            )
            assert skipped.status_code == 200, skipped.text
        else:
            await _mark(client, headers, run["id"], stop["id"])
    finished = await client.post(f"{API}/{run['id']}/complete", headers=headers)
    assert finished.status_code == 200, finished.text
    body: dict[str, Any] = finished.json()
    return body, headers


async def _set_threshold(value: int | None) -> None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM runtime_settings WHERE key = 'af_counted_stops_percent'")
            )
            if value is not None:
                await conn.execute(
                    text(
                        "INSERT INTO runtime_settings (key, value, updated_at) "
                        "VALUES ('af_counted_stops_percent', :value, now())"
                    ),
                    {"value": str(value)},
                )
    finally:
        await engine.dispose()


async def test_the_threshold_decides_and_is_fixed_when_the_run_ends(
    live_client: AsyncClient,
) -> None:
    route_id = await _route_with_stops(live_client, minimum=4)
    try:
        half, half_headers = await _walk_skipping(live_client, route_id, {1, 2})
        share = half["completed_share_percent"]
        assert share < 70
        assert half["counted"] is False
        assert half["awarded_points"] > 0

        # The editors lower the threshold: it applies to new runs only.
        await _set_threshold(50)
        later, _ = await _walk_skipping(live_client, route_id, {1, 2})
        assert later["completed_share_percent"] == share
        assert (later["counted"], later["counted_threshold_percent"]) == (share >= 50, 50)
        earlier = await live_client.get(f"{API}/{half['id']}", headers=half_headers)
        assert earlier.json()["counted"] is False
    finally:
        await _set_threshold(None)


async def test_only_the_walked_way_counts_towards_kilometres(live_client: AsyncClient) -> None:
    route_id = await _route_with_stops(live_client, minimum=4)
    whole, _ = await _walk_skipping(live_client, route_id, set())
    partial, _ = await _walk_skipping(live_client, route_id, {1, 2})
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            rows = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT id::text, paid_distance_meters FROM route_executions "
                            "WHERE id = :whole OR id = :partial"
                        ),
                        {"whole": whole["id"], "partial": partial["id"]},
                    )
                ).all()
            )
    finally:
        await engine.dispose()
    assert rows[whole["id"]] == whole["routing"]["distance_meters"]
    assert 0 <= rows[partial["id"]] < rows[whole["id"]]


async def test_a_partial_walker_may_rate_but_stays_out_of_the_average(
    live_client: AsyncClient,
) -> None:
    route_id = await _route_with_stops(live_client, minimum=4)
    reviews_url = f"/api/v1/routes/{route_id}/reviews"
    before = (await live_client.get(reviews_url, params={"limit": 50})).json()

    _whole, walker = await _walk_skipping(live_client, route_id, set())
    _half, partial = await _walk_skipping(live_client, route_id, {1, 2})
    stranger = await _headers(live_client)

    full_review = await live_client.post(reviews_url, json={"rating": 5}, headers=walker)
    partial_review = await live_client.post(reviews_url, json={"rating": 1}, headers=partial)
    refused = await live_client.post(reviews_url, json={"rating": 3}, headers=stranger)

    assert full_review.status_code == 200, full_review.text
    assert (full_review.json()["author_walk"], full_review.json()["author_completed_route"]) == (
        "full",
        True,
    )
    assert partial_review.status_code == 200, partial_review.text
    assert partial_review.json()["author_walk"] == "partial"
    assert partial_review.json()["author_completed_route"] is False
    # Stars without a word are for those who walked at least a part.
    assert refused.status_code == 422

    after = (await live_client.get(reviews_url, params={"limit": 50})).json()
    marks = {item["id"]: item["author_walk"] for item in after["items"]}
    assert marks[full_review.json()["id"]] == "full"
    assert marks[partial_review.json()["id"]] == "partial"
    # Both reviews are listed; only the walker's five stars moved the rating.
    assert after["rating_count"] == before["rating_count"] + 1
    total_before = (before["average_rating"] or 0) * before["rating_count"]
    assert after["average_rating"] == pytest.approx(
        (total_before + 5) / after["rating_count"], abs=0.06
    )
