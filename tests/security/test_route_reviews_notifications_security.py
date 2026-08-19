"""Route reviews + in-app notifications security regressions."""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.notifications.infrastructure.models import Notification
from tourism_backend.modules.routes.application import review_media
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
    assert any(
        n["kind"] == "review_published" and n["target_id"] == route_id
        for n in foreign.json()["items"]
    )

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
async def test_review_unpublished_route_is_rejected(live_client: AsyncClient) -> None:
    auth = await _login(live_client, "+79001110108", "ЧерновикЮзер")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

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
        assert route is not None
        route_id = route.id
        previous = route.publication_status
        route.publication_status = "pending_review"
        await session.commit()
    await engine.dispose()

    try:
        listed = await live_client.get(f"/api/v1/routes/{route_id}/reviews")
        assert listed.status_code == 404

        created = await live_client.post(
            f"/api/v1/routes/{route_id}/reviews",
            headers=headers,
            json={"body": "Отзыв на черновик", "rating": 5},
        )
        assert created.status_code == 409
        body = created.json()["error"]
        assert body["code"] == "route_not_published"
        assert "неопубликованные" in body["message"].lower()
    finally:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        async with session_maker() as session:
            restored = await session.get(Route, route_id)
            assert restored is not None
            restored.publication_status = previous
            await session.commit()
        await engine.dispose()


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
async def test_reject_review_notifies_author_not_owner(live_client: AsyncClient) -> None:
    owner = await _login(live_client, "+79001110107", "РеджектОвнер")
    author = await _login(live_client, "+79001110118", "РеджектАвтор")
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
    author_inbox = await live_client.get("/api/v1/me/notifications", headers=author_headers)
    assert author_inbox.status_code == 200
    assert any(
        n["kind"] == "review_rejected" and n["target_id"] == route_id
        for n in author_inbox.json()["items"]
    )
    public = await live_client.get(f"/api/v1/routes/{route_id}/reviews")
    assert all(item["id"] != str(review_id) for item in public.json()["items"])


@pytest.mark.asyncio
async def test_route_moderation_notifies_owner(live_client: AsyncClient) -> None:
    from tourism_backend.modules.notifications.application import (
        service as notifications_service,
    )

    owner = await _login(live_client, "+79001110119", "МодМаршрутОвнер")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
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
        route_id = route.id
        route_name = route.name
        published = await notifications_service.create_route_moderation_notification(
            session,
            owner_user_id=owner_id,
            route_id=route_id,
            route_name=route_name,
            approved=True,
        )
        rejected = await notifications_service.create_route_moderation_notification(
            session,
            owner_user_id=owner_id,
            route_id=route_id,
            route_name=route_name,
            approved=False,
        )
        await session.commit()
        published_id = str(published.id)
        rejected_id = str(rejected.id)
    await engine.dispose()

    inbox = await live_client.get("/api/v1/me/notifications", headers=headers)
    assert inbox.status_code == 200
    kinds = {item["id"]: item["kind"] for item in inbox.json()["items"]}
    assert kinds[published_id] == "route_published"
    assert kinds[rejected_id] == "route_rejected"
    assert all(
        item["target_type"] == "route" and item["target_id"] == str(route_id)
        for item in inbox.json()["items"]
        if item["id"] in {published_id, rejected_id}
    )


async def _public_route_id() -> str | None:
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
    return route_id


@pytest.mark.asyncio
async def test_second_review_keeps_published_and_creates_pending(
    live_client: AsyncClient,
) -> None:
    route_id = await _public_route_id()
    if route_id is None:
        pytest.skip("No public route seeded")

    marker = uuid4().hex[:8]
    auth = await _login(
        live_client,
        phone=f"+7900{uuid4().int % 10_000_000:07d}",
        name=f"МультиОтзыв {marker}",
    )
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    first = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=headers,
        json={"body": "Первый отзыв автора на маршрут", "rating": 5},
    )
    assert first.status_code == 200, first.text
    first_id = UUID(first.json()["id"])

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        changed = await set_review_status(
            session,
            review_ids=[first_id],
            status="published",
        )
        await session.commit()
        assert changed == 1
    await engine.dispose()

    second = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=headers,
        json={"body": "Второй отзыв без перезаписи первого", "rating": 4},
    )
    assert second.status_code == 200, second.text
    second_id = second.json()["id"]
    assert second_id != str(first_id)
    assert second.json()["status"] == "pending_review"

    public = await live_client.get(f"/api/v1/routes/{route_id}/reviews")
    assert public.status_code == 200
    published_item = next(item for item in public.json()["items"] if item["id"] == str(first_id))
    assert published_item["body"] == "Первый отзыв автора на маршрут"
    assert all(item["id"] != second_id for item in public.json()["items"])

    # Updating while pending keeps the same pending row.
    again = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=headers,
        json={"body": "Правка pending без новой строки", "rating": 3},
    )
    assert again.status_code == 200
    assert again.json()["id"] == second_id
    assert again.json()["body"] == "Правка pending без новой строки"

    # Published row must stay untouched after pending edits.
    public_after = await live_client.get(f"/api/v1/routes/{route_id}/reviews")
    still = next(item for item in public_after.json()["items"] if item["id"] == str(first_id))
    assert still["body"] == "Первый отзыв автора на маршрут"
    assert still["status"] == "published"


@pytest.mark.asyncio
async def test_review_reply_keeps_quote_and_notifies_target_after_moderation(
    live_client: AsyncClient,
) -> None:
    route_id = await _public_route_id()
    if route_id is None:
        pytest.skip("No public route seeded")
    marker = uuid4().hex[:8]
    target = await _login(
        live_client,
        phone=f"+7910{uuid4().int % 10_000_000:07d}",
        name=f"Адресат {marker}",
    )
    responder = await _login(
        live_client,
        phone=f"+7911{uuid4().int % 10_000_000:07d}",
        name=f"Ответчик {marker}",
    )
    target_headers = {"Authorization": f"Bearer {target['access_token']}"}
    responder_headers = {"Authorization": f"Bearer {responder['access_token']}"}

    original = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=target_headers,
        json={"body": "Исходный отзыв для ответа", "rating": 5},
    )
    assert original.status_code == 200, original.text
    original_id = UUID(original.json()["id"])
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        assert (
            await set_review_status(
                session,
                review_ids=[original_id],
                status="published",
            )
            == 1
        )
        await session.commit()

    reply = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=responder_headers,
        json={
            "body": "Ответ с сохранённым контекстом",
            "rating": 5,
            "reply_to_review_id": str(original_id),
        },
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["reply_to"]["review_id"] == str(original_id)
    assert reply.json()["reply_to"]["body"] == "Исходный отзыв для ответа"

    invalid = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=responder_headers,
        json={
            "body": "Ответ в никуда",
            "rating": 4,
            "reply_to_review_id": str(uuid4()),
        },
    )
    assert invalid.status_code == 404
    assert invalid.json()["error"]["code"] == "review_reply_target_not_found"

    async with session_maker() as session:
        assert (
            await set_review_status(
                session,
                review_ids=[UUID(reply.json()["id"])],
                status="published",
            )
            == 1
        )
        await session.commit()
    await engine.dispose()

    public = await live_client.get(f"/api/v1/routes/{route_id}/reviews")
    published_reply = next(
        item for item in public.json()["items"] if item["id"] == reply.json()["id"]
    )
    assert published_reply["reply_to"]["author_display_name"] == f"Адресат {marker}"

    inbox = await live_client.get("/api/v1/me/notifications", headers=target_headers)
    assert any(
        item["kind"] == "review_reply" and item["target_id"] == route_id
        for item in inbox.json()["items"]
    )


@pytest.mark.asyncio
async def test_delete_own_review_within_window_and_rejects_foreign(
    live_client: AsyncClient,
) -> None:
    from datetime import UTC, datetime, timedelta

    from tourism_backend.modules.routes.infrastructure.models import RouteReview

    route_id = await _public_route_id()
    if route_id is None:
        pytest.skip("No public route seeded")

    marker = uuid4().hex[:8]
    author = await _login(
        live_client,
        phone=f"+7900{uuid4().int % 10_000_000:07d}",
        name=f"УдалОтзыв {marker}",
    )
    other = await _login(
        live_client,
        phone=f"+7900{uuid4().int % 10_000_000:07d}",
        name=f"ЧужойУдал {marker}",
    )
    author_headers = {"Authorization": f"Bearer {author['access_token']}"}
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    created = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=author_headers,
        json={"body": "Отзыв который можно удалить", "rating": 5},
    )
    assert created.status_code == 200, created.text
    review_id = created.json()["id"]

    unauth = await live_client.delete(f"/api/v1/routes/{route_id}/reviews/{review_id}")
    assert unauth.status_code == 401

    foreign = await live_client.delete(
        f"/api/v1/routes/{route_id}/reviews/{review_id}",
        headers=other_headers,
    )
    assert foreign.status_code == 404

    ok = await live_client.delete(
        f"/api/v1/routes/{route_id}/reviews/{review_id}",
        headers=author_headers,
    )
    assert ok.status_code == 204

    mine = await live_client.get("/api/v1/me/reviews", headers=author_headers)
    assert all(item["id"] != review_id for item in mine.json()["items"])

    expired = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=author_headers,
        json={"body": "Старый отзыв вне окна удаления", "rating": 4},
    )
    assert expired.status_code == 200
    expired_id = UUID(expired.json()["id"])

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        row = await session.get(RouteReview, expired_id)
        assert row is not None
        row.created_at = datetime.now(UTC) - timedelta(hours=7)
        await session.commit()
    await engine.dispose()

    late = await live_client.delete(
        f"/api/v1/routes/{route_id}/reviews/{expired_id}",
        headers=author_headers,
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "review_delete_window_expired"
    assert "6 часов" in late.json()["error"]["message"]


@pytest.mark.asyncio
async def test_review_photo_upload_listing_and_owner_only_delete(
    live_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_media, "_MEDIA_ROOT", tmp_path)
    route_id = await _public_route_id()
    if route_id is None:
        pytest.skip("No public route seeded")

    marker = uuid4().hex[:8]
    author = await _login(
        live_client,
        phone=f"+7902{uuid4().int % 10_000_000:07d}",
        name=f"ФотоАвтор {marker}",
    )
    other = await _login(
        live_client,
        phone=f"+7903{uuid4().int % 10_000_000:07d}",
        name=f"ФотоЧужой {marker}",
    )
    author_headers = {"Authorization": f"Bearer {author['access_token']}"}
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    created = await live_client.post(
        f"/api/v1/routes/{route_id}/reviews",
        headers=author_headers,
        json={"body": "Отзыв с безопасным фото", "rating": 5},
    )
    assert created.status_code == 200, created.text
    review_id = created.json()["id"]

    payload = io.BytesIO()
    Image.new("RGB", (80, 60), color=(30, 100, 140)).save(payload, format="PNG")
    upload_url = f"/api/v1/routes/{route_id}/reviews/{review_id}/media"
    unauth = await live_client.post(
        upload_url,
        data={"position": "0"},
        files={"file": ("photo.png", payload.getvalue(), "image/png")},
    )
    assert unauth.status_code == 401

    uploaded = await live_client.post(
        upload_url,
        headers=author_headers,
        data={"position": "0"},
        files={"file": ("photo.png", payload.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    photo = uploaded.json()
    assert photo["url"].startswith(f"/media/reviews/{review_id}/")
    assert photo["width"] == 80
    assert photo["height"] == 60

    mine = await live_client.get("/api/v1/me/reviews", headers=author_headers)
    item = next(row for row in mine.json()["items"] if row["id"] == review_id)
    assert item["media"] == [photo]

    delete_url = f"{upload_url}/{photo['id']}"
    foreign = await live_client.delete(delete_url, headers=other_headers)
    assert foreign.status_code == 404
    deleted = await live_client.delete(delete_url, headers=author_headers)
    assert deleted.status_code == 204

    mine_after = await live_client.get("/api/v1/me/reviews", headers=author_headers)
    item_after = next(row for row in mine_after.json()["items"] if row["id"] == review_id)
    assert item_after["media"] == []


@pytest.mark.asyncio
async def test_profile_like_notifies_target_once(live_client: AsyncClient) -> None:
    marker = uuid4().hex[:8]
    target = await _login(
        live_client,
        phone=f"+7906{uuid4().int % 10_000_000:07d}",
        name=f"ЛайкЦель {marker}",
    )
    liker = await _login(
        live_client,
        phone=f"+7905{uuid4().int % 10_000_000:07d}",
        name=f"Лайкер {marker}",
    )
    target_headers = {"Authorization": f"Bearer {target['access_token']}"}
    liker_headers = {"Authorization": f"Bearer {liker['access_token']}"}

    target_me = await live_client.get("/api/v1/me", headers=target_headers)
    liker_me = await live_client.get("/api/v1/me", headers=liker_headers)
    target_id = target_me.json()["id"]
    liker_id = liker_me.json()["id"]

    before = await live_client.get("/api/v1/me/notifications", headers=target_headers)
    before_count = before.json()["unread_count"]

    liked = await live_client.put(
        f"/api/v1/users/{target_id}/like",
        headers=liker_headers,
    )
    assert liked.status_code == 204, liked.text

    inbox = await live_client.get("/api/v1/me/notifications", headers=target_headers)
    assert inbox.status_code == 200
    assert inbox.json()["unread_count"] == before_count + 1
    matches = [
        n
        for n in inbox.json()["items"]
        if n["kind"] == "profile_like" and n["actor_user_id"] == liker_id
    ]
    assert len(matches) == 1
    assert matches[0]["target_type"] == "user"
    assert matches[0]["target_id"] == liker_id

    liker_inbox = await live_client.get("/api/v1/me/notifications", headers=liker_headers)
    assert all(n["kind"] != "profile_like" for n in liker_inbox.json()["items"])

    # Idempotent re-like does not create another notification.
    again = await live_client.put(
        f"/api/v1/users/{target_id}/like",
        headers=liker_headers,
    )
    assert again.status_code == 204
    after = await live_client.get("/api/v1/me/notifications", headers=target_headers)
    assert sum(1 for n in after.json()["items"] if n["kind"] == "profile_like") == sum(
        1 for n in inbox.json()["items"] if n["kind"] == "profile_like"
    )

    unlike = await live_client.delete(
        f"/api/v1/users/{target_id}/like",
        headers=liker_headers,
    )
    assert unlike.status_code == 204
    after_unlike = await live_client.get("/api/v1/me/notifications", headers=target_headers)
    assert after_unlike.json()["unread_count"] == after.json()["unread_count"]
