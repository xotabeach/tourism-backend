"""Spec 19: the nightly popularity recalculation against a real database."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tourism_backend.modules.popularity import recalc
from tourism_backend.modules.popularity.policy import MIN_ACCOUNT_AGE_DAYS

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)


@pytest.fixture
def session() -> Iterator[Session]:
    sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg")
    try:
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM place_view_events LIMIT 1"))
    except Exception:  # noqa: BLE001
        pytest.skip("Postgres for integration tests is unavailable")
    with Session(engine) as db:
        yield db
        db.rollback()
    engine.dispose()


def _user(
    db: Session,
    *,
    age_days: int = 30,
    system: bool = False,
    internal: bool = False,
) -> UUID:
    user_id = uuid4()
    created = datetime.now(UTC) - timedelta(days=age_days)
    db.execute(
        text(
            "INSERT INTO users (id, display_name, phone_e164, created_at, updated_at, "
            "is_system_account, is_internal_account) "
            "VALUES (:id, 'Популярность', :phone, :created, :created, :system, :internal)"
        ),
        {
            "id": user_id,
            "phone": f"+7905{uuid4().int % 10_000_000:07d}",
            "created": created,
            "system": system,
            "internal": internal,
        },
    )
    return user_id


def _published(db: Session, table: str) -> UUID:
    extra = "AND visibility = 'public' AND lifecycle_status = 'active'" if table == "routes" else ""
    row = db.execute(
        text(f"SELECT id FROM {table} WHERE publication_status = 'published' {extra} LIMIT 1")  # noqa: S608
    ).first()
    if row is None:
        pytest.skip(f"no published {table} in the test database")
    return UUID(str(row[0]))


def test_recalculation_counts_only_eligible_people_and_writes_the_result(
    session: Session,
) -> None:
    route_id = _published(session, "routes")
    place_id = _published(session, "places")
    regulars = [_user(session) for _ in range(3)]
    outsiders = [
        _user(session, system=True),
        _user(session, internal=True),
        _user(session, age_days=MIN_ACCOUNT_AGE_DAYS - 1),
    ]
    for user_id in [*regulars, *outsiders]:
        session.execute(
            text(
                "INSERT INTO favorite_routes (user_id, route_id, created_at) VALUES (:u, :r, now())"
            ),
            {"u": user_id, "r": route_id},
        )
        session.execute(
            text("INSERT INTO place_view_events (user_id, place_id, day) VALUES (:u, :p, :d)"),
            {"u": user_id, "p": place_id, "d": date.today()},
        )
    session.flush()

    report = recalc.compute(session)

    route = report.routes[route_id]
    assert route.people >= len(regulars)
    assert route.popularity > 0
    place = report.places[place_id]
    assert place.people >= len(regulars)
    eligible = recalc.eligible_user_ids(session, now=datetime.now(UTC))
    assert set(regulars) <= eligible
    assert not eligible & set(outsiders)

    recalc.apply(session, report)
    session.flush()
    stored = session.execute(
        text("SELECT popularity, popularity_people, is_popular FROM routes WHERE id = :id"),
        {"id": route_id},
    ).one()
    assert stored.popularity == pytest.approx(route.popularity)
    assert stored.popularity_people == route.people
    assert stored.is_popular is route.is_popular
    assert (
        session.execute(
            text("SELECT popularity_updated_at FROM places WHERE id = :id"), {"id": place_id}
        ).scalar_one()
        is not None
    )


def test_recalculation_resets_what_is_no_longer_scored(session: Session) -> None:
    route_id = _published(session, "routes")
    session.execute(
        text("UPDATE routes SET popularity = 77, popularity_people = 5, is_popular = true"),
    )
    session.execute(text("DELETE FROM favorite_routes"))
    session.execute(text("DELETE FROM route_executions"))
    session.execute(text("DELETE FROM route_reviews"))
    session.flush()

    report = recalc.compute(session)
    recalc.apply(session, report)
    session.flush()

    assert route_id not in report.routes
    stored = session.execute(
        text("SELECT popularity, popularity_people, is_popular FROM routes WHERE id = :id"),
        {"id": route_id},
    ).one()
    assert (stored.popularity, stored.popularity_people, stored.is_popular) == (0, 0, False)


# --- the API side: an opened place card and the badge fields -----------------

from collections.abc import AsyncIterator  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from tourism_backend.config import Settings  # noqa: E402
from tourism_backend.db.redis import create_redis_client  # noqa: E402
from tourism_backend.main import create_app  # noqa: E402

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


@pytest.fixture
async def live_client() -> AsyncIterator[AsyncClient]:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1 FROM place_view_events LIMIT 1"))
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


async def _login(client: AsyncClient) -> dict[str, str]:
    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    requested = await client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Просмотр", "phone": phone},
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


async def test_place_view_is_recorded_once_a_day_and_needs_a_login(
    live_client: AsyncClient,
) -> None:
    places = await live_client.get("/api/v1/places", params={"limit": 1})
    assert places.status_code == 200, places.text
    place = places.json()["items"][0]
    assert "badge" in place
    url = f"/api/v1/places/{place['id']}/view"

    assert (await live_client.post(url)).status_code == 401

    headers = await _login(live_client)
    first = await live_client.post(url, headers=headers)
    again = await live_client.post(url, headers=headers)
    assert first.status_code == 204, first.text
    assert again.status_code == 204, again.text

    missing = await live_client.post(f"/api/v1/places/{uuid4()}/view", headers=headers)
    assert missing.status_code == 404

    routes = await live_client.get("/api/v1/routes", params={"limit": 5, "sort": "popular"})
    assert routes.status_code == 200, routes.text
    for route in routes.json()["items"]:
        assert route["badge"] in {None, "popular", "editors_choice"}
        if route["source"] == "editorial":
            assert route["badge"] in {"popular", "editors_choice"}
