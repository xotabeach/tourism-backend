"""Deleting and clearing notifications, end to end against Postgres and Redis."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.notifications.application.retention import (
    any_cutoff,
    expired_notification_ids,
    read_cutoff,
)

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


@pytest.fixture
async def db() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    yield engine
    await engine.dispose()


async def _login(client: AsyncClient) -> tuple[dict[str, str], UUID]:
    phone = f"+7901{uuid4().int % 10_000_000:07d}"
    requested = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Чистильщик", "phone": phone},
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
    headers = {"Authorization": f"Bearer {verified.json()['access_token']}"}
    me = await client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200, me.text
    user_id = UUID(me.json()["id"])
    # Signing up may leave a welcome notification; the tests count their own.
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM notifications WHERE user_id = :u"), {"u": user_id})
    await engine.dispose()
    return headers, user_id


async def _add(
    engine: AsyncEngine,
    user_id: UUID,
    *,
    read: bool = False,
    age: timedelta = timedelta(0),
    at: datetime | None = None,
) -> UUID:
    notification_id = uuid4()
    created = at or (datetime.now(UTC) - age)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO notifications (id, user_id, kind, title, body, is_read, created_at) "
                "VALUES (:id, :u, 'support_reply', 'Заголовок', 'Текст', :read, :at)"
            ),
            {"id": notification_id, "u": user_id, "read": read, "at": created},
        )
    return notification_id


async def _count(engine: AsyncEngine, user_id: UUID, **where: Any) -> int:
    sql = "SELECT count(*) FROM notifications WHERE user_id = :u"
    if "read" in where:
        sql += " AND is_read = :read"
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), {"u": user_id, **where})).scalar() or 0)


async def _exists(engine: AsyncEngine, notification_id: UUID) -> bool:
    async with engine.connect() as conn:
        found = (
            await conn.execute(
                text("SELECT count(*) FROM notifications WHERE id = :id"), {"id": notification_id}
            )
        ).scalar()
    return bool(found)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


@pytest.mark.asyncio
async def test_deleting_one_is_idempotent_and_never_touches_another_persons_row(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    headers, user_id = await _login(live_client)
    _other_headers, other_id = await _login(live_client)
    mine = await _add(db, user_id)
    theirs = await _add(db, other_id)

    first = await live_client.delete(f"/api/v1/me/notifications/{mine}", headers=headers)
    again = await live_client.delete(f"/api/v1/me/notifications/{mine}", headers=headers)
    foreign = await live_client.delete(f"/api/v1/me/notifications/{theirs}", headers=headers)
    unknown = await live_client.delete(f"/api/v1/me/notifications/{uuid4()}", headers=headers)

    assert {first.status_code, again.status_code, foreign.status_code, unknown.status_code} == {204}
    assert not await _exists(db, mine)
    assert await _exists(db, theirs), "somebody else's row must survive"


@pytest.mark.asyncio
async def test_a_queued_batch_deletes_only_the_callers_own_rows(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    headers, user_id = await _login(live_client)
    _other_headers, other_id = await _login(live_client)
    own = [await _add(db, user_id) for _ in range(3)]
    theirs = await _add(db, other_id)

    response = await live_client.post(
        "/api/v1/me/notifications/delete",
        headers=headers,
        json={"ids": [str(item) for item in [*own, theirs]]},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": 3}
    assert await _exists(db, theirs)
    too_many = await live_client.post(
        "/api/v1/me/notifications/delete",
        headers=headers,
        json={"ids": [str(uuid4()) for _ in range(101)]},
    )
    assert too_many.status_code == 422
    empty = await live_client.post(
        "/api/v1/me/notifications/delete", headers=headers, json={"ids": []}
    )
    assert empty.status_code == 422


@pytest.mark.asyncio
async def test_clearing_read_keeps_unread_and_anything_newer_than_before(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    headers, user_id = await _login(live_client)
    loaded_at = datetime.now(UTC) - timedelta(minutes=5)
    old_read = await _add(db, user_id, read=True, at=loaded_at - timedelta(days=2))
    old_unread = await _add(db, user_id, read=False, at=loaded_at - timedelta(days=2))
    arrived_after = await _add(db, user_id, read=True, at=loaded_at + timedelta(minutes=1))

    response = await live_client.delete(
        "/api/v1/me/notifications",
        headers=headers,
        params={"scope": "read", "before": _iso(loaded_at)},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": 1}
    assert not await _exists(db, old_read)
    assert await _exists(db, old_unread)
    assert await _exists(db, arrived_after), "arrived after the list was loaded"


@pytest.mark.asyncio
async def test_clearing_all_removes_what_the_screen_never_showed_but_not_newer_ones(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    headers, user_id = await _login(live_client)
    loaded_at = datetime.now(UTC) - timedelta(minutes=5)
    for index in range(60):  # the app only ever shows the newest 50
        await _add(db, user_id, read=index % 2 == 0, at=loaded_at - timedelta(minutes=index))
    fresh = await _add(db, user_id, at=loaded_at + timedelta(seconds=30))

    response = await live_client.delete(
        "/api/v1/me/notifications",
        headers=headers,
        params={"scope": "all", "before": _iso(loaded_at)},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": 60}
    assert await _count(db, user_id) == 1
    assert await _exists(db, fresh)
    inbox = await live_client.get("/api/v1/me/notifications", headers=headers)
    assert inbox.json()["unread_count"] == 1


@pytest.mark.asyncio
async def test_a_dry_run_only_counts(live_client: AsyncClient, db: AsyncEngine) -> None:
    headers, user_id = await _login(live_client)
    boundary = datetime.now(UTC) - timedelta(minutes=1)
    for _ in range(4):
        await _add(db, user_id, read=True, at=boundary - timedelta(hours=1))

    response = await live_client.delete(
        "/api/v1/me/notifications",
        headers=headers,
        params={"scope": "read", "before": _iso(boundary), "dry_run": "true"},
    )

    assert response.json() == {"deleted": 4}
    assert await _count(db, user_id) == 4


@pytest.mark.asyncio
async def test_the_bulk_endpoint_needs_a_valid_scope_and_a_boundary(
    live_client: AsyncClient,
) -> None:
    headers, _user = await _login(live_client)
    now = _iso(datetime.now(UTC))
    assert (
        await live_client.delete("/api/v1/me/notifications", headers=headers)
    ).status_code == 422
    assert (
        await live_client.delete(
            "/api/v1/me/notifications", headers=headers, params={"scope": "all"}
        )
    ).status_code == 422
    assert (
        await live_client.delete(
            "/api/v1/me/notifications",
            headers=headers,
            params={"scope": "everything", "before": now},
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_deleting_needs_a_signed_in_person(live_client: AsyncClient) -> None:
    assert (await live_client.delete(f"/api/v1/me/notifications/{uuid4()}")).status_code in {
        401,
        403,
    }
    assert (
        await live_client.post("/api/v1/me/notifications/delete", json={"ids": [str(uuid4())]})
    ).status_code in {401, 403}


@pytest.mark.asyncio
async def test_bulk_clearing_is_rate_limited_per_person(live_client: AsyncClient) -> None:
    headers, _user = await _login(live_client)
    params = {"scope": "read", "before": _iso(datetime.now(UTC)), "dry_run": "true"}
    statuses = [
        (
            await live_client.delete("/api/v1/me/notifications", headers=headers, params=params)
        ).status_code
        for _ in range(12)
    ]
    assert statuses[:10] == [200] * 10
    assert statuses[10:] == [429, 429]
    # A single delete has its own, much larger allowance.
    single = await live_client.delete(f"/api/v1/me/notifications/{uuid4()}", headers=headers)
    assert single.status_code == 204


@pytest.mark.asyncio
async def test_the_retention_query_picks_read_after_90_days_and_any_after_180(
    live_client: AsyncClient, db: AsyncEngine
) -> None:
    _headers, user_id = await _login(live_client)
    read_old = await _add(db, user_id, read=True, age=timedelta(days=100))
    unread_old = await _add(db, user_id, read=False, age=timedelta(days=100))
    unread_ancient = await _add(db, user_id, read=False, age=timedelta(days=200))
    read_recent = await _add(db, user_id, read=True, age=timedelta(days=30))

    async with db.connect() as conn:
        found = {
            row[0]
            for row in (
                await conn.execute(
                    expired_notification_ids(
                        read_before=read_cutoff(), any_before=any_cutoff(), limit=100_000
                    )
                )
            ).all()
        }

    assert read_old in found
    assert unread_ancient in found
    assert unread_old not in found, "unread is kept longer"
    assert read_recent not in found
