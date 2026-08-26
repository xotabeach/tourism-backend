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
        await conn.execute(
            text("UPDATE users SET travel_points = 4999 WHERE id = :id"),
            {"id": low_id},
        )
        await conn.execute(
            text("UPDATE users SET travel_points = 5000 WHERE id = :id"),
            {"id": high_id},
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
        await conn.execute(
            text("UPDATE users SET travel_points = 100 WHERE id = :id"),
            {"id": regular_id},
        )
        await conn.execute(
            text("UPDATE users SET travel_points = 999999, is_expert = true WHERE id = :id"),
            {"id": expert_id},
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
    assert body["total"] >= 15
    assert 5 <= body["unlocked_count"] <= 15
    assert body["unlocked_count"] <= body["total"]
    assert len(body["items"]) == body["total"]
    for item in body["items"]:
        assert set(item) == {
            "id",
            "slug",
            "title",
            "description",
            "is_unlocked",
            "unlocked_at",
        }
        assert len(item["title"]) <= 120
        assert len(item["description"]) <= 240
        if item["is_unlocked"]:
            assert item["unlocked_at"] is not None
        else:
            assert item["unlocked_at"] is None

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
    assert len(unlocked) == 1
    assert unlocked[0]["title"] == "Новое достижение"
    assert unlocked[0]["target_type"] == "achievement"
    assert unlocked[0]["target_id"]
    assert "phone" not in str(unlocked[0]).lower()
