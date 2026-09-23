"""Public user profile + attachment ownership security regressions."""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
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


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_public_profile_hides_phone_and_is_readable(
    live_client: AsyncClient,
) -> None:
    phone = f"+7905{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone, name="Публичный")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    user_id = me.json()["id"]

    upload = await live_client.post(
        "/api/v1/me/avatar",
        headers=headers,
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 200, upload.text

    public = await live_client.get(f"/api/v1/users/{user_id}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["id"] == user_id
    assert body["display_name"] == "Публичный"
    assert body["avatar_url"]
    assert "phone" not in body
    assert "phone_e164" not in body
    assert isinstance(body["travel_points"], int)
    assert isinstance(body["liked_by_me"], bool)
    assert isinstance(body["is_expert"], bool)
    assert body["rank_slug"]
    assert body["rank_title"]
    assert isinstance(body["next_rank_points"], int)
    assert isinstance(body["leaderboard_place"], int)
    assert body["leaderboard_place"] >= 1
    assert isinstance(body["followers_count"], int)
    assert body["followers_count"] >= 0
    assert isinstance(body["following_count"], int)
    assert body["following_count"] >= 0
    assert set(body.keys()) == {
        "id",
        "display_name",
        "avatar_url",
        "cover_url",
        "travel_points",
        "rank_slug",
        "rank_title",
        "next_rank_points",
        "leaderboard_place",
        "liked_by_me",
        "is_expert",
        "followers_count",
        "following_count",
        "completed_routes_count",
        # Public activity counters, deliberately added 2026-09-04: they say
        # what a traveller has done, never who they are.
        "published_routes_count",
        "published_articles_count",
        "article_likes_count",
        "reviews_written_count",
        "total_distance_meters",
    }


@pytest.mark.asyncio
async def test_public_user_not_found(live_client: AsyncClient) -> None:
    missing = await live_client.get(f"/api/v1/users/{uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_public_user_search_returns_profile_media_without_pii(
    live_client: AsyncClient,
) -> None:
    marker = uuid4().hex[:8]
    display_name = f"Искатель {marker}"
    await _login(
        live_client,
        phone=f"+7907{uuid4().int % 10_000_000:07d}",
        name=display_name,
    )

    response = await live_client.get(
        "/api/v1/users/search",
        params={"q": marker, "limit": 5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 1
    found = next(item for item in body["items"] if item["display_name"] == display_name)
    assert set(found) == {
        "id",
        "display_name",
        "avatar_url",
        "cover_url",
        "travel_points",
        "rank_slug",
        "rank_title",
        "next_rank_points",
        "leaderboard_place",
        "liked_by_me",
        "is_expert",
        "followers_count",
        "following_count",
        "completed_routes_count",
        # Public activity counters, deliberately added 2026-09-04: they say
        # what a traveller has done, never who they are.
        "published_routes_count",
        "published_articles_count",
        "article_likes_count",
        "reviews_written_count",
        "total_distance_meters",
    }
    assert "phone" not in str(found).lower()


@pytest.mark.asyncio
async def test_users_leaderboard_is_public_and_ordered_by_points(
    live_client: AsyncClient,
) -> None:
    marker = uuid4().hex[:6]
    low = await _login(
        live_client,
        phone=f"+7908{uuid4().int % 10_000_000:07d}",
        name=f"Low {marker}",
    )
    high = await _login(
        live_client,
        phone=f"+7909{uuid4().int % 10_000_000:07d}",
        name=f"High {marker}",
    )
    low_id = (
        await live_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {low['access_token']}"},
        )
    ).json()["id"]
    high_id = (
        await live_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {high['access_token']}"},
        )
    ).json()["id"]

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        top = int(
            (
                await conn.execute(text("SELECT COALESCE(MAX(travel_points), 0) FROM users"))
            ).scalar_one()
        )
        low_pts = top + 1
        high_pts = top + 2
        await conn.execute(
            text("UPDATE users SET travel_points = :points WHERE id = :id"),
            {"id": low_id, "points": low_pts},
        )
        await conn.execute(
            text("UPDATE users SET travel_points = :points WHERE id = :id"),
            {"id": high_id, "points": high_pts},
        )
    await engine.dispose()

    response = await live_client.get(
        "/api/v1/users/leaderboard",
        params={"limit": 100, "offset": 0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] >= 2
    ids = [item["id"] for item in body["items"]]
    assert high_id in ids
    assert low_id in ids
    assert ids.index(high_id) < ids.index(low_id)
    high_row = next(item for item in body["items"] if item["id"] == high_id)
    assert high_row["rank_slug"]
    assert high_row["rank_title"]
    if 5000 <= high_pts < 10_000:
        assert high_row["rank_slug"] == "explorer"
        assert high_row["rank_title"] == "Исследователь"
    assert "phone" not in str(high_row).lower()
    assert set(high_row) == {
        "id",
        "display_name",
        "avatar_url",
        "cover_url",
        "travel_points",
        "rank_slug",
        "rank_title",
        "next_rank_points",
        "leaderboard_place",
        "liked_by_me",
        "is_expert",
        "followers_count",
        "following_count",
        "completed_routes_count",
        # Public activity counters, deliberately added 2026-09-04: they say
        # what a traveller has done, never who they are.
        "published_routes_count",
        "published_articles_count",
        "article_likes_count",
        "reviews_written_count",
        "total_distance_meters",
    }
    oversized = await live_client.get(
        "/api/v1/users/leaderboard",
        params={"limit": 101},
    )
    assert oversized.status_code == 422


@pytest.mark.asyncio
async def test_users_leaderboard_excludes_experts(
    live_client: AsyncClient,
) -> None:
    """Experts accrue points far faster than regular travelers and would
    dominate every leaderboard slot, defeating its purpose as a ranking for
    ordinary users."""
    marker = uuid4().hex[:6]
    regular = await _login(
        live_client,
        phone=f"+7910{uuid4().int % 10_000_000:07d}",
        name=f"Regular {marker}",
    )
    expert = await _login(
        live_client,
        phone=f"+7911{uuid4().int % 10_000_000:07d}",
        name=f"Expert {marker}",
    )
    regular_id = (
        await live_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {regular['access_token']}"},
        )
    ).json()["id"]
    expert_id = (
        await live_client.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {expert['access_token']}"},
        )
    ).json()["id"]

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        # The integration database is intentionally persistent between local
        # runs. Give the fixture a score above the current maximum so the
        # assertion tests expert exclusion rather than depending on an empty
        # leaderboard or the first 100 rows from previous runs.
        highest_points = await conn.scalar(
            text("SELECT COALESCE(MAX(travel_points), 0) FROM users")
        )
        regular_points = int(highest_points or 0) + 1
        await conn.execute(
            text("UPDATE users SET travel_points = :points WHERE id = :id"),
            {"id": regular_id, "points": regular_points},
        )
        await conn.execute(
            text(
                "UPDATE users SET travel_points = :points, is_expert = true, "
                "rank_id = '00000000-0000-0000-0000-000000000106' WHERE id = :id"
            ),
            {"id": expert_id, "points": regular_points + 1},
        )
    await engine.dispose()

    response = await live_client.get(
        "/api/v1/users/leaderboard",
        params={"limit": 100, "offset": 0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    ids = [item["id"] for item in body["items"]]
    assert regular_id in ids
    assert expert_id not in ids


@pytest.mark.asyncio
async def test_profile_subscriptions_return_liked_users_without_pii(
    live_client: AsyncClient,
) -> None:
    marker = uuid4().hex[:8]
    target_tokens = await _login(
        live_client,
        phone=f"+7906{uuid4().int % 10_000_000:07d}",
        name=f"Автор {marker}",
    )
    target_headers = {"Authorization": f"Bearer {target_tokens['access_token']}"}
    target_me = await live_client.get("/api/v1/me", headers=target_headers)
    assert target_me.status_code == 200, target_me.text
    target_id = target_me.json()["id"]
    reader_tokens = await _login(
        live_client,
        phone=f"+7905{uuid4().int % 10_000_000:07d}",
        name=f"Читатель {marker}",
    )
    reader_headers = {"Authorization": f"Bearer {reader_tokens['access_token']}"}
    liked = await live_client.put(
        f"/api/v1/users/{target_id}/like",
        headers=reader_headers,
    )
    assert liked.status_code == 204, liked.text

    response = await live_client.get(
        "/api/v1/users/subscriptions",
        headers=reader_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    found = next(item for item in body["items"] if item["id"] == target_id)
    assert found["liked_by_me"] is True
    assert "phone" not in str(found).lower()

    target_public = await live_client.get(f"/api/v1/users/{target_id}")
    assert target_public.status_code == 200, target_public.text
    assert target_public.json()["followers_count"] >= 1
    reader_me = await live_client.get("/api/v1/me", headers=reader_headers)
    reader_id = reader_me.json()["id"]
    reader_public = await live_client.get(f"/api/v1/users/{reader_id}")
    assert reader_public.json()["following_count"] >= 1


@pytest.mark.asyncio
async def test_profile_followers_are_public_searchable_and_without_pii(
    live_client: AsyncClient,
) -> None:
    marker = uuid4().hex[:6]
    target = await _login(
        live_client, phone=f"+7907{uuid4().int % 10_000_000:07d}", name=f"Цель {marker}"
    )
    target_headers = {"Authorization": f"Bearer {target['access_token']}"}
    target_id = (await live_client.get("/api/v1/me", headers=target_headers)).json()["id"]
    names = [f"Анна {marker}", f"Борис {marker}"]
    for name in names:
        follower = await _login(
            live_client, phone=f"+7908{uuid4().int % 10_000_000:07d}", name=name
        )
        liked = await live_client.put(
            f"/api/v1/users/{target_id}/like",
            headers={"Authorization": f"Bearer {follower['access_token']}"},
        )
        assert liked.status_code == 204, liked.text

    # Readable without an account, like the follower count itself.
    everyone = await live_client.get(f"/api/v1/users/{target_id}/followers")
    assert everyone.status_code == 200, everyone.text
    body = everyone.json()
    assert body["total"] == 2
    # Newest first.
    assert [item["display_name"] for item in body["items"]] == list(reversed(names))
    assert "phone" not in str(body).lower()
    assert all(item["liked_by_me"] is False for item in body["items"])

    narrowed = await live_client.get(f"/api/v1/users/{target_id}/followers", params={"q": "анна"})
    assert [item["display_name"] for item in narrowed.json()["items"]] == [names[0]]
    assert narrowed.json()["total"] == 1

    missing = await live_client.get(f"/api/v1/users/{uuid4()}/followers")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_routes_catalog_includes_owner_fields_when_present(
    live_client: AsyncClient,
) -> None:
    response = await live_client.get("/api/v1/routes", params={"region_slug": "crimea", "limit": 5})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "items" in payload
    for item in payload["items"]:
        assert "owner_user_id" in item
        assert "author_avatar_url" in item
        assert "author_label" in item
        assert isinstance(item["author_is_expert"], bool)


@pytest.mark.asyncio
async def test_public_user_routes_endpoint(live_client: AsyncClient) -> None:
    phone = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone, name="Автор")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    user_id = me.json()["id"]

    routes = await live_client.get(f"/api/v1/users/{user_id}/routes")
    assert routes.status_code == 200, routes.text
    body = routes.json()
    assert "items" in body
    assert body["total"] >= 0
    for item in body["items"]:
        assert item.get("owner_user_id") == user_id


@pytest.mark.asyncio
async def test_achievements_catalog_is_public_and_bounded(live_client: AsyncClient) -> None:
    tokens = await _login(
        live_client,
        phone=f"+7904{uuid4().int % 10_000_000:07d}",
        name="Достигатор",
    )
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    user_id = me.json()["id"]

    catalog = await live_client.get(f"/api/v1/users/{user_id}/achievements")
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    assert body == {"items": [], "total": 0, "unlocked_count": 0}
    private = await live_client.get("/api/v1/me/achievements", headers=headers)
    assert private.status_code == 200, private.text
    assert len(private.json()["items"]) == 32
    assert private.json()["unlocked_count"] == 0
    assert private.json()["total"] < 32
    assert (await live_client.get("/api/v1/me/achievements")).status_code == 401

    missing = await live_client.get(f"/api/v1/users/{uuid4()}/achievements")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_achievement_unlock_notifies_owner_inbox(live_client: AsyncClient) -> None:
    tokens = await _login(
        live_client,
        phone=f"+7903{uuid4().int % 10_000_000:07d}",
        name="Новичок",
    )
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    inbox = await live_client.get("/api/v1/me/notifications", headers=headers)
    assert inbox.status_code == 200, inbox.text
    unlocked = [item for item in inbox.json()["items"] if item["kind"] == "achievement_unlocked"]
    assert unlocked == []


@pytest.mark.asyncio
async def test_public_profile_reports_completed_routes_reviews_and_distance(
    live_client: AsyncClient,
) -> None:
    """Workstream F: profile activity stats come from real completed
    executions and published reviews — not just published-routes count."""
    tokens = await _login(
        live_client,
        phone=f"+7903{uuid4().int % 10_000_000:07d}",
        name="Активный турист",
    )
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    user_id = me.json()["id"]

    before = await live_client.get(f"/api/v1/users/{user_id}")
    assert before.status_code == 200, before.text
    assert before.json()["completed_routes_count"] == 0
    assert before.json()["reviews_written_count"] == 0
    assert before.json()["total_distance_meters"] == 0

    catalog = await live_client.get("/api/v1/routes", params={"limit": 1})
    assert catalog.status_code == 200, catalog.text
    route_id = catalog.json()["items"][0]["id"]

    started = await live_client.post(
        "/api/v1/route-executions",
        json={"route_id": route_id},
        headers=headers,
    )
    assert started.status_code == 201, started.text
    execution = started.json()
    for stop in execution["stops"]:
        completed_stop = await live_client.put(
            f"/api/v1/route-executions/{execution['id']}/stops/{stop['id']}/complete",
            headers=headers,
        )
        assert completed_stop.status_code == 200, completed_stop.text
    finished = await live_client.post(
        f"/api/v1/route-executions/{execution['id']}/complete",
        headers=headers,
    )
    assert finished.status_code == 200, finished.text

    review = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=headers,
        json={"body": "Прошёл маршрут, очень понравилось", "rating": 5},
    )
    assert review.status_code in {200, 201}, review.text
    review_id = review.json()["id"]

    # Fresh reviews start pending_review; publish it directly like a
    # moderator would, so the count reflects only what's actually public.
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE route_reviews SET status = 'published' WHERE id = :id"),
            {"id": review_id},
        )
        snapshot_id = (
            await conn.execute(
                text("SELECT routing_snapshot_id FROM route_executions WHERE id = :id"),
                {"id": execution["id"]},
            )
        ).scalar_one()
        expected_distance = (
            await conn.execute(
                text("SELECT distance_meters FROM route_routing_snapshots WHERE id = :id"),
                {"id": snapshot_id},
            )
        ).scalar_one()
    await engine.dispose()

    after = await live_client.get(f"/api/v1/users/{user_id}")
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["completed_routes_count"] == 1
    assert body["reviews_written_count"] == 1
    assert body["total_distance_meters"] == (expected_distance or 0)


@pytest.mark.asyncio
async def test_achievement_grant_race_privacy_and_celebration(live_client: AsyncClient):
    import asyncio
    from datetime import UTC, datetime
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from tourism_backend.modules.achievements.service import evaluate
    from tourism_backend.modules.content.infrastructure.models import Article
    from tourism_backend.modules.identity.infrastructure.models import UserAchievement
    from tourism_backend.modules.notifications.infrastructure.models import Notification

    tokens = await _login(live_client, phone=f"+7911{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    user_id = UUID((await live_client.get("/api/v1/me", headers=headers)).json()["id"])
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add(
                Article(
                    id=uuid4(),
                    author_user_id=user_id,
                    title="Путевые заметки",
                    status="published",
                    published_at=datetime.now(UTC),
                )
            )
            await session.commit()

        async def grant():
            async with factory() as session:
                badges = await evaluate(session, user_id, {"article"})
                await session.commit()
                return badges

        results = await asyncio.gather(grant(), grant())
        assert sum(map(len, results)) == 1
        async with factory() as session:
            award = (
                await session.scalars(
                    select(UserAchievement).where(UserAchievement.user_id == user_id)
                )
            ).one()
            badge_id = str(award.achievement_id)
            assert award.source == "rule"
            assert award.celebrated_at is None
            notices = (
                await session.scalars(
                    select(Notification).where(
                        Notification.user_id == user_id, Notification.kind == "achievement_unlocked"
                    )
                )
            ).all()
            assert len(notices) == 1
        public = (await live_client.get(f"/api/v1/users/{user_id}/achievements")).json()
        assert len(public["items"]) == 1
        assert "unlocked_at" not in public["items"][0]
        assert "progress" not in public["items"][0]
        private = await live_client.get(
            "/api/v1/me/achievements?uncelebrated=true", headers=headers
        )
        assert [item["id"] for item in private.json()["items"]] == [badge_id]
        for _ in range(2):
            response = await live_client.post(
                "/api/v1/me/achievements/celebrated",
                headers=headers,
                json={"achievement_ids": [badge_id]},
            )
            assert response.status_code == 204
        assert (
            await live_client.get("/api/v1/me/achievements?uncelebrated=true", headers=headers)
        ).json()["items"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_achievement_backfill_and_flag_gate(live_client: AsyncClient):
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from tourism_backend.modules.achievements.service import evaluate
    from tourism_backend.modules.content.infrastructure.models import Article
    from tourism_backend.modules.identity.infrastructure.models import UserAchievement
    from tourism_backend.modules.notifications.infrastructure.models import Notification
    from tourism_backend.modules.route_execution.infrastructure.models import UserFraudState

    tokens = await _login(live_client, phone=f"+7912{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    user_id = UUID((await live_client.get("/api/v1/me", headers=headers)).json()["id"])
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    published = datetime.now(UTC) - timedelta(days=10)
    try:
        async with factory() as session:
            session.add(
                Article(
                    id=uuid4(),
                    author_user_id=user_id,
                    title="Путевые заметки",
                    status="published",
                    published_at=published,
                )
            )
            state = UserFraudState(user_id=user_id, is_flagged=True, updated_at=datetime.now(UTC))
            session.add(state)
            await session.commit()
            assert await evaluate(session, user_id, backfill=True) == []
            state.is_flagged = False
            await session.commit()
            assert len(await evaluate(session, user_id, backfill=True)) == 1
            await session.commit()
            assert await evaluate(session, user_id, backfill=True) == []
            row = (
                await session.scalars(
                    select(UserAchievement).where(UserAchievement.user_id == user_id)
                )
            ).one()
            assert row.source == "backfill"
            assert row.unlocked_at == row.celebrated_at == published
            assert not (
                await session.scalars(
                    select(Notification).where(
                        Notification.user_id == user_id, Notification.kind == "achievement_unlocked"
                    )
                )
            ).all()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manual_achievement_actions_are_audited_and_soon_rejected(live_client: AsyncClient):
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from tourism_backend.api.errors import AppError
    from tourism_backend.modules.achievements.admin_actions import apply
    from tourism_backend.modules.admin.infrastructure.models import AdminPrincipal
    from tourism_backend.modules.identity.infrastructure.models import (
        Achievement,
        AchievementAdminAction,
        UserAchievement,
    )
    from tourism_backend.modules.notifications.infrastructure.models import Notification

    tokens = await _login(live_client, phone=f"+7914{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    user_id = UUID((await live_client.get("/api/v1/me", headers=headers)).json()["id"])
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            admin = AdminPrincipal(id=uuid4(), login=f"test-{uuid4()}", password_hash="test-only")
            session.add(admin)
            await session.commit()
            badge = (
                await session.scalars(select(Achievement).where(Achievement.slug == "pen"))
            ).one()
            soon = (
                await session.scalars(select(Achievement).where(Achievement.slug == "group"))
            ).one()
            with pytest.raises(AppError, match="") as error:
                await apply(
                    session,
                    user_id=user_id,
                    achievement_id=soon.id,
                    admin_id=admin.id,
                    action="grant",
                    reason="Проверка",
                )
            assert error.value.code == "achievement_soon"
            await session.rollback()
            # ORM values expire on rollback; keep only stable fixture ids below.
            admin = (
                await session.scalars(
                    select(AdminPrincipal)
                    .where(AdminPrincipal.login.like("test-%"))
                    .order_by(AdminPrincipal.created_at.desc())
                )
            ).first()
            badge = (
                await session.scalars(select(Achievement).where(Achievement.slug == "pen"))
            ).one()
            assert admin is not None
            admin_id, badge_id = admin.id, badge.id
            for _ in range(2):
                await apply(
                    session,
                    user_id=user_id,
                    achievement_id=badge_id,
                    admin_id=admin_id,
                    action="grant",
                    reason="Подтверждено редактором",
                )
            row = await session.get(UserAchievement, (user_id, badge_id))
            assert row is not None
            assert row.source == "operator"
            assert row.reason == "Подтверждено редактором"
            assert (
                len(
                    (
                        await session.scalars(
                            select(Notification).where(
                                Notification.user_id == user_id,
                                Notification.kind == "achievement_unlocked",
                            )
                        )
                    ).all()
                )
                == 1
            )
            await apply(
                session,
                user_id=user_id,
                achievement_id=badge_id,
                admin_id=admin_id,
                action="revoke",
                reason="Исправление",
            )
            assert await session.get(UserAchievement, (user_id, badge_id)) is None
            assert not (
                await session.scalars(
                    select(Notification).where(
                        Notification.user_id == user_id, Notification.kind == "achievement_unlocked"
                    )
                )
            ).all()
            assert (
                len(
                    (
                        await session.scalars(
                            select(AchievementAdminAction).where(
                                AchievementAdminAction.user_id == user_id
                            )
                        )
                    ).all()
                )
                == 3
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reset_backfill_exports_before_deleting_and_is_repeatable(
    live_client: AsyncClient, tmp_path
):
    import csv
    from uuid import UUID

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession

    from tourism_backend.modules.achievements.maintenance import backfill
    from tourism_backend.modules.identity.infrastructure.models import UserAchievement
    from tourism_backend.modules.notifications.infrastructure.models import Notification

    tokens = await _login(live_client, phone=f"+7913{uuid4().int % 10_000_000:07d}")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    user_id = UUID((await live_client.get("/api/v1/me", headers=headers)).json()["id"])
    # Re-evaluating every user of a shared test database grows with each run;
    # the reset itself still covers all grants.
    scope = [user_id]
    engine = create_async_engine(DATABASE_URL)
    try:
        async with AsyncSession(engine) as session:
            before = await session.scalar(select(func.count()).select_from(UserAchievement))
            count = await backfill(session, reset=True, backup_dir=tmp_path, user_ids=scope)
            assert count >= 0
            backups = list(tmp_path.glob("achievements-*/user_achievements.csv"))
            assert len(backups) == 1
            with backups[0].open() as source:
                assert len(list(csv.DictReader(source))) == before
            assert backups[0].stat().st_mode & 0o777 == 0o600
            assert await backfill(session, user_ids=scope) == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.kind == "achievement_unlocked")
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(UserAchievement)
                    .where(UserAchievement.celebrated_at.is_(None))
                )
                == 0
            )
            # This integration test exercises the full reset transaction but
            # rolls back instead of disturbing other tests' committed fixtures.
            await session.rollback()
    finally:
        await engine.dispose()
