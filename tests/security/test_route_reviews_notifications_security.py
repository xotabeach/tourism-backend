"""Route reviews + in-app notifications security regressions."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.notifications.infrastructure.models import Notification
from tourism_backend.modules.routes.application.review_service import set_review_status
from tourism_backend.modules.routes.infrastructure.models import Route

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


async def _login(client: AsyncClient, phone: str, name: str) -> dict:
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


@pytest.mark.asyncio
async def test_review_requires_auth_and_hides_pending(live_client: AsyncClient) -> None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        route = await session.scalar(
            select(Route)
            .where(
                Route.visibility == "public",
                Route.lifecycle_status == "active",
                Route.publication_status == "published",
            )
            .limit(1)
        )
        route_id = str(route.id) if route is not None else None
    await engine.dispose()
    if route_id is None:
        pytest.skip("No public route seeded")

    unauth = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        json={"body": "Отличный маршрут для выходных", "rating": 5},
    )
    assert unauth.status_code == 401

    auth = await _login(live_client, "+79001110101", "Рецензент")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    bad = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=headers,
        json={"body": "x" * 2001, "rating": 5},
    )
    assert bad.status_code == 422

    xss = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=headers,
        json={"body": "<script>alert(1)</script> красивый вид", "rating": 4},
    )
    assert xss.status_code == 200, xss.text
    assert xss.json()["status"] == "pending_review"
    assert "<script>" in xss.json()["body"]

    listed = await live_client.get(f"/api/v1/routes/{route_id}/reviews")
    assert listed.status_code == 200
    assert all(item["id"] != xss.json()["id"] for item in listed.json()["items"])

    mine = await live_client.get("/api/v1/me/reviews", headers=headers)
    assert mine.status_code == 200
    assert any(item["id"] == xss.json()["id"] for item in mine.json()["items"])


@pytest.mark.asyncio
async def test_approve_review_notifies_owner_and_inbox_is_private(
    live_client: AsyncClient,
) -> None:
    owner = await _login(live_client, "+79001110102", "ВладелецМаршрута")
    author = await _login(live_client, "+79001110103", "АвторОтзыва")
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    author_headers = {"Authorization": f"Bearer {author['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=owner_headers)
    assert me.status_code == 200
    owner_id = UUID(me.json()["id"])

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        route = await session.scalar(
            select(Route)
            .where(
                Route.visibility == "public",
                Route.lifecycle_status == "active",
                Route.publication_status == "published",
            )
            .limit(1)
        )
        if route is None:
            await engine.dispose()
            pytest.skip("No public route seeded")
        owner_user = await session.get(User, owner_id)
        assert owner_user is not None
        route.owner_user_id = owner_user.id
        await session.commit()
        route_id = str(route.id)

    created = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=author_headers,
        json={"body": "Маршрут понравился всей семье", "rating": 5},
    )
    assert created.status_code == 200, created.text
    review_id = UUID(created.json()["id"])

    async with session_maker() as session:
        changed = await set_review_status(
            session,
            review_ids=[review_id],
            status="published",
        )
        await session.commit()
        assert changed == 1
        notif = await session.scalar(
            select(Notification)
            .where(
                Notification.user_id == owner_id,
                Notification.target_id == UUID(route_id),
                Notification.kind == "route_review",
            )
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
        assert notif is not None
        assert notif.target_type == "route"
        notif_id = str(notif.id)

    await engine.dispose()

    inbox = await live_client.get("/api/v1/me/notifications", headers=owner_headers)
    assert inbox.status_code == 200
    assert inbox.json()["unread_count"] >= 1
    assert any(n["id"] == notif_id for n in inbox.json()["items"])

    foreign = await live_client.get("/api/v1/me/notifications", headers=author_headers)
    assert foreign.status_code == 200
    assert all(n["id"] != notif_id for n in foreign.json()["items"])

    public = await live_client.get(f"/api/v1/routes/{route_id}/reviews")
    assert public.status_code == 200
    assert any(item["id"] == str(review_id) for item in public.json()["items"])
    assert public.json()["rating_count"] >= 1

    steal = await live_client.post(
        f"/api/v1/me/notifications/{notif_id}/read",
        headers=author_headers,
    )
    assert steal.status_code == 404

    mark = await live_client.post(
        f"/api/v1/me/notifications/{notif_id}/read",
        headers=owner_headers,
    )
    assert mark.status_code == 200
    assert mark.json()["is_read"] is True


@pytest.mark.asyncio
async def test_review_missing_route_is_404(live_client: AsyncClient) -> None:
    missing = await live_client.get(f"/api/v1/routes/{uuid4()}/reviews")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_device_tokens_require_auth_and_round_trip(live_client: AsyncClient) -> None:
    token = "fcm-test-token-" + ("x" * 40)
    unauth = await live_client.post(
        "/api/v1/me/device-tokens",
        json={"token": token, "platform": "android"},
    )
    assert unauth.status_code == 401

    auth = await _login(live_client, "+79001110104", "ПушЮзер")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    bad = await live_client.post(
        "/api/v1/me/device-tokens",
        headers=headers,
        json={"token": "short", "platform": "android"},
    )
    assert bad.status_code == 422

    ok = await live_client.post(
        "/api/v1/me/device-tokens",
        headers=headers,
        json={"token": token, "platform": "ios"},
    )
    assert ok.status_code == 204

    # Upsert same token under same user (platform change).
    again = await live_client.post(
        "/api/v1/me/device-tokens",
        headers=headers,
        json={"token": token, "platform": "android"},
    )
    assert again.status_code == 204

    deleted = await live_client.request(
        "DELETE",
        "/api/v1/me/device-tokens",
        headers=headers,
        json={"token": token},
    )
    assert deleted.status_code == 204

    # Deleting again is idempotent.
    deleted_again = await live_client.request(
        "DELETE",
        "/api/v1/me/device-tokens",
        headers=headers,
        json={"token": token},
    )
    assert deleted_again.status_code == 204


@pytest.mark.asyncio
async def test_mark_all_notifications_read(live_client: AsyncClient) -> None:
    owner = await _login(live_client, "+79001110105", "ИнбоксОвнер")
    author = await _login(live_client, "+79001110106", "ИнбоксАвтор")
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    author_headers = {"Authorization": f"Bearer {author['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=owner_headers)
    owner_id = UUID(me.json()["id"])

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        route = await session.scalar(
            select(Route)
            .where(
                Route.visibility == "public",
                Route.lifecycle_status == "active",
                Route.publication_status == "published",
            )
            .limit(1)
        )
        if route is None:
            await engine.dispose()
            pytest.skip("No public route seeded")
        route.owner_user_id = owner_id
        await session.commit()
        route_id = str(route.id)

    created = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=author_headers,
        json={"body": "Отзыв для read-all", "rating": 4},
    )
    assert created.status_code == 200
    review_id = UUID(created.json()["id"])

    async with session_maker() as session:
        await set_review_status(session, review_ids=[review_id], status="published")
        await session.commit()
    await engine.dispose()

    inbox = await live_client.get("/api/v1/me/notifications", headers=owner_headers)
    assert inbox.json()["unread_count"] >= 1

    marked = await live_client.post(
        "/api/v1/me/notifications/read-all",
        headers=owner_headers,
    )
    assert marked.status_code == 200
    assert marked.json()["updated"] >= 1

    after = await live_client.get("/api/v1/me/notifications", headers=owner_headers)
    assert after.json()["unread_count"] == 0


@pytest.mark.asyncio
async def test_reject_review_does_not_notify(live_client: AsyncClient) -> None:
    owner = await _login(live_client, "+79001110107", "РеджектОвнер")
    author = await _login(live_client, "+79001110108", "РеджектАвтор")
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    author_headers = {"Authorization": f"Bearer {author['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=owner_headers)
    owner_id = UUID(me.json()["id"])

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        route = await session.scalar(
            select(Route)
            .where(
                Route.visibility == "public",
                Route.lifecycle_status == "active",
                Route.publication_status == "published",
            )
            .limit(1)
        )
        if route is None:
            await engine.dispose()
            pytest.skip("No public route seeded")
        route.owner_user_id = owner_id
        await session.commit()
        route_id = str(route.id)

    before = await live_client.get("/api/v1/me/notifications", headers=owner_headers)
    before_count = before.json()["unread_count"]

    created = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=author_headers,
        json={"body": "Отзыв на отклонение модератором", "rating": 2},
    )
    assert created.status_code == 200
    review_id = UUID(created.json()["id"])

    async with session_maker() as session:
        changed = await set_review_status(
            session,
            review_ids=[review_id],
            status="rejected",
        )
        await session.commit()
        assert changed == 1
    await engine.dispose()

    after = await live_client.get("/api/v1/me/notifications", headers=owner_headers)
    assert after.json()["unread_count"] == before_count
    public = await live_client.get(f"/api/v1/routes/{route_id}/reviews")
    assert all(item["id"] != str(review_id) for item in public.json()["items"])
