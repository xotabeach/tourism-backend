"""Auth and favorites security/integration regressions."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.identity.infrastructure.models import AuthOtpChallenge
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


async def _login(client: AsyncClient, phone: str = "+79001234567", name: str = "Тестер") -> dict:
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
async def test_me_requires_auth(live_client: AsyncClient) -> None:
    response = await live_client.get("/api/v1/me")
    assert response.status_code == 401
    assert "access_token" not in response.text.lower() or "error" in response.text


@pytest.mark.asyncio
async def test_otp_rejects_oversized_and_sqli_phone(live_client: AsyncClient) -> None:
    oversized = await live_client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "A", "phone": "7" * 200},
    )
    assert oversized.status_code == 422

    sqli = await live_client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "A", "phone": "'; DROP TABLE users;--"},
    )
    assert sqli.status_code == 422


@pytest.mark.asyncio
async def test_otp_flow_and_me(live_client: AsyncClient) -> None:
    phone = f"+7900{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone, name="Никита")
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    body = me.json()
    assert body["display_name"] == "Никита"
    assert body["phone"] == phone
    assert "access_token" not in me.text


@pytest.mark.asyncio
async def test_phone_first_registration_then_login_without_repeated_consents(
    live_client: AsyncClient,
) -> None:
    phone = f"+7908{uuid4().int % 10_000_000:07d}"

    first = await live_client.post("/api/v1/auth/otp/start", json={"phone": phone})
    assert first.status_code == 200, first.text
    assert first.json() == {
        "registration_required": True,
        "consents_required": True,
        "otp_sent": False,
    }

    registration = await live_client.post(
        "/api/v1/auth/otp/start",
        json={"phone": phone, "display_name": "Новый путник"},
    )
    assert registration.status_code == 200, registration.text
    assert registration.json()["otp_sent"] is True
    assert registration.json()["consents_required"] is True
    created = await live_client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": "1234",
            "privacy_accepted": True,
            "personal_data_accepted": True,
        },
    )
    assert created.status_code == 200, created.text

    login = await live_client.post(
        "/api/v1/auth/otp/start",
        json={"phone": phone, "display_name": "Не менять имя"},
    )
    assert login.status_code == 200, login.text
    assert login.json() == {
        "registration_required": False,
        "consents_required": False,
        "otp_sent": True,
    }
    signed_in = await live_client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": "1234",
            "privacy_accepted": False,
            "personal_data_accepted": False,
        },
    )
    assert signed_in.status_code == 200, signed_in.text
    headers = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}
    me = await live_client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["display_name"] == "Новый путник"


@pytest.mark.asyncio
async def test_refresh_rotation_and_reuse_detection(live_client: AsyncClient) -> None:
    phone = f"+7901{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone)
    first_refresh = tokens["refresh_token"]

    rotated = await live_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": first_refresh},
    )
    assert rotated.status_code == 200
    new_refresh = rotated.json()["refresh_token"]
    assert new_refresh != first_refresh

    reuse = await live_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": first_refresh},
    )
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "refresh_reuse"

    after_reuse = await live_client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": new_refresh},
    )
    assert after_reuse.status_code == 401


@pytest.mark.asyncio
async def test_concurrent_refresh_does_not_issue_two_rotations(
    live_client: AsyncClient,
) -> None:
    """M-4: two in-flight refreshes of the same token must not both succeed."""
    phone = f"+7901{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone)
    first_refresh = tokens["refresh_token"]

    first, second = await asyncio.gather(
        live_client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh}),
        live_client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh}),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 401], (first.text, second.text)
    winner = first if first.status_code == 200 else second
    loser = second if first.status_code == 200 else first
    assert winner.json()["refresh_token"] != first_refresh
    assert loser.json()["error"]["code"] in {"refresh_reuse", "refresh_invalid"}


@pytest.mark.asyncio
async def test_favorites_bola_and_unpublished(live_client: AsyncClient) -> None:
    phone_a = f"+7902{uuid4().int % 10_000_000:07d}"
    phone_b = f"+7903{uuid4().int % 10_000_000:07d}"
    tokens_a = await _login(live_client, phone=phone_a, name="A")
    tokens_b = await _login(live_client, phone=phone_b, name="B")
    headers_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {tokens_b['access_token']}"}

    places = await live_client.get("/api/v1/places?limit=1")
    assert places.status_code == 200
    items = places.json()["items"]
    if not items:
        pytest.skip("seed places unavailable")
    place_id = items[0]["id"]

    routes = await live_client.get("/api/v1/routes?limit=1")
    assert routes.status_code == 200
    route_items = routes.json()["items"]
    if not route_items:
        pytest.skip("seed routes unavailable")
    route_id = route_items[0]["id"]

    put_place = await live_client.put(f"/api/v1/favorites/places/{place_id}", headers=headers_a)
    assert put_place.status_code == 204
    put_route = await live_client.put(f"/api/v1/favorites/routes/{route_id}", headers=headers_a)
    assert put_route.status_code == 204

    fav_a = await live_client.get("/api/v1/favorites", headers=headers_a)
    assert fav_a.status_code == 200
    assert place_id in fav_a.json()["place_ids"]
    assert route_id in fav_a.json()["route_ids"]

    fav_b = await live_client.get("/api/v1/favorites", headers=headers_b)
    assert fav_b.status_code == 200
    assert place_id not in fav_b.json()["place_ids"]
    assert route_id not in fav_b.json()["route_ids"]

    # Unknown / unpublished targets are not enumerable as favorites.
    denied = await live_client.put(f"/api/v1/favorites/places/{uuid4()}", headers=headers_a)
    assert denied.status_code == 404

    unauth = await live_client.get("/api/v1/favorites")
    assert unauth.status_code == 401


@pytest.mark.asyncio
async def test_favorite_route_respects_catalog_publication_status(
    live_client: AsyncClient,
) -> None:
    """M-3: public+active is not enough — unpublished catalog rows must 404/hide."""
    phone = f"+7902{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone, name="Каталог")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    routes = await live_client.get("/api/v1/routes?limit=1")
    items = routes.json().get("items") or []
    if not items:
        pytest.skip("seed routes unavailable")
    route_id = items[0]["id"]

    added = await live_client.put(f"/api/v1/favorites/routes/{route_id}", headers=headers)
    assert added.status_code == 204
    listed = await live_client.get("/api/v1/favorites", headers=headers)
    assert route_id in listed.json()["route_ids"]

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    previous: str | None = None
    try:
        async with factory() as session:
            route = await session.get(Route, UUID(route_id))
            assert route is not None
            previous = route.publication_status
            route.publication_status = "rejected"
            await session.commit()

        hidden = await live_client.get("/api/v1/favorites", headers=headers)
        assert hidden.status_code == 200
        assert route_id not in hidden.json()["route_ids"]

        denied = await live_client.put(f"/api/v1/favorites/routes/{route_id}", headers=headers)
        assert denied.status_code == 404
    finally:
        async with factory() as session:
            route = await session.get(Route, UUID(route_id))
            if route is not None and previous is not None:
                route.publication_status = previous
                await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_otp_request_keeps_a_single_live_code(live_client: AsyncClient) -> None:
    phone = f"+7904{uuid4().int % 10_000_000:07d}"
    payload = {"display_name": "Никита", "phone": phone}
    first, second = await asyncio.gather(
        live_client.post("/api/v1/auth/otp/request", json=payload),
        live_client.post("/api/v1/auth/otp/request", json=payload),
    )
    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text
    third = await live_client.post("/api/v1/auth/otp/request", json=payload)
    assert third.status_code == 204, third.text

    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            count = await conn.scalar(
                select(func.count())
                .select_from(AuthOtpChallenge)
                .where(
                    AuthOtpChallenge.phone_e164 == phone,
                    AuthOtpChallenge.consumed_at.is_(None),
                    AuthOtpChallenge.expires_at > datetime.now(UTC),
                )
            )
    finally:
        await engine.dispose()
    assert count == 1
