"""Profile / support security regressions."""

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
async def test_patch_name_and_media(live_client: AsyncClient) -> None:
    phone = f"+7902{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone, name="Старое")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    patched = await live_client.patch(
        "/api/v1/me",
        headers=headers,
        json={"display_name": "Новое Имя"},
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Новое Имя"

    upload = await live_client.post(
        "/api/v1/me/avatar",
        headers=headers,
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 200, upload.text
    avatar_url = upload.json()["avatar_url"]
    assert avatar_url.startswith("/media/profiles/")
    assert avatar_url.endswith(".webp")

    bad = await live_client.post(
        "/api/v1/me/cover",
        headers=headers,
        files={"file": ("x.bin", b"not-an-image" + b"0" * 100, "application/octet-stream")},
    )
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_phone_change_flow(live_client: AsyncClient) -> None:
    phone = f"+7903{uuid4().int % 10_000_000:07d}"
    new_phone = f"+7904{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    req = await live_client.post(
        "/api/v1/me/phone/otp/request",
        headers=headers,
        json={"phone": new_phone},
    )
    assert req.status_code == 204, req.text

    verify = await live_client.post(
        "/api/v1/me/phone/otp/verify",
        headers=headers,
        json={
            "phone": new_phone,
            "code": "1234",
            "privacy_accepted": True,
            "personal_data_accepted": True,
        },
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["phone"] == new_phone


@pytest.mark.asyncio
async def test_support_ticket_ownership(live_client: AsyncClient) -> None:
    phone_a = f"+7905{uuid4().int % 10_000_000:07d}"
    phone_b = f"+7906{uuid4().int % 10_000_000:07d}"
    tokens_a = await _login(live_client, phone=phone_a, name="A")
    tokens_b = await _login(live_client, phone=phone_b, name="B")
    headers_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {tokens_b['access_token']}"}

    created = await live_client.post(
        "/api/v1/support/tickets",
        headers=headers_a,
        json={
            "kind": "chat",
            "subject": "Помощь",
            "body": "Здравствуйте, нужна помощь с маршрутом",
        },
    )
    assert created.status_code == 200, created.text
    ticket = created.json()
    assert len(ticket["messages"]) >= 2
    ticket_id = ticket["id"]

    foreign = await live_client.get(
        f"/api/v1/support/tickets/{ticket_id}",
        headers=headers_b,
    )
    assert foreign.status_code == 404

    oversized = await live_client.post(
        "/api/v1/support/tickets",
        headers=headers_a,
        json={
            "kind": "app_error",
            "subject": "x",
            "body": "x" * 5000,
        },
    )
    assert oversized.status_code == 422

    sqli = await live_client.post(
        f"/api/v1/support/tickets/{ticket_id}/messages",
        headers=headers_a,
        json={"body": "'; DROP TABLE support_tickets;--"},
    )
    assert sqli.status_code == 200
    assert sqli.json()["body"].startswith("';")

    unauth = await live_client.get("/api/v1/support/tickets")
    assert unauth.status_code == 401


@pytest.mark.asyncio
async def test_support_ticket_attachments(live_client: AsyncClient) -> None:
    phone_a = f"+7907{uuid4().int % 10_000_000:07d}"
    phone_b = f"+7908{uuid4().int % 10_000_000:07d}"
    tokens_a = await _login(live_client, phone=phone_a, name="A")
    tokens_b = await _login(live_client, phone=phone_b, name="B")
    headers_a = {"Authorization": f"Bearer {tokens_a['access_token']}"}
    headers_b = {"Authorization": f"Bearer {tokens_b['access_token']}"}

    created = await live_client.post(
        "/api/v1/support/tickets",
        headers=headers_a,
        json={
            "kind": "app_error",
            "subject": "Вылетает",
            "body": "Приложение вылетает на главном экране",
        },
    )
    assert created.status_code == 200, created.text
    ticket_id = created.json()["id"]

    foreign = await live_client.post(
        f"/api/v1/support/tickets/{ticket_id}/attachments",
        headers=headers_b,
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )
    assert foreign.status_code == 404

    bad = await live_client.post(
        f"/api/v1/support/tickets/{ticket_id}/attachments",
        headers=headers_a,
        files={"file": ("x.bin", b"not-an-image" + b"0" * 100, "application/octet-stream")},
    )
    assert bad.status_code == 400

    uploaded = await live_client.post(
        f"/api/v1/support/tickets/{ticket_id}/attachments",
        headers=headers_a,
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    assert attachment["url"].startswith("/media/support/")
    assert attachment["url"].endswith(".webp")

    fetched = await live_client.get(
        f"/api/v1/support/tickets/{ticket_id}",
        headers=headers_a,
    )
    assert fetched.status_code == 200, fetched.text
    assert [item["id"] for item in fetched.json()["attachments"]] == [attachment["id"]]

    for _ in range(2):
        extra = await live_client.post(
            f"/api/v1/support/tickets/{ticket_id}/attachments",
            headers=headers_a,
            files={"file": ("a.png", _png_bytes(), "image/png")},
        )
        assert extra.status_code == 200, extra.text

    over_limit = await live_client.post(
        f"/api/v1/support/tickets/{ticket_id}/attachments",
        headers=headers_a,
        files={"file": ("a.png", _png_bytes(), "image/png")},
    )
    assert over_limit.status_code == 409


@pytest.mark.asyncio
async def test_patch_preferences(live_client: AsyncClient) -> None:
    phone = f"+7909{uuid4().int % 10_000_000:07d}"
    tokens = await _login(live_client, phone=phone)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    before = await live_client.get("/api/v1/me", headers=headers)
    assert before.status_code == 200, before.text
    assert before.json()["preferred_categories"] == []
    assert before.json()["preferences_updated_at"] is None

    patched = await live_client.patch(
        "/api/v1/me/preferences",
        headers=headers,
        json={
            "preferred_categories": ["Море", "Горы"],
            "preferred_difficulty": "moderate",
            "travels_with_kids": True,
            "travels_with_pets": False,
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["preferred_categories"] == ["Море", "Горы"]
    assert body["preferred_difficulty"] == "moderate"
    assert body["travels_with_kids"] is True
    assert body["travels_with_pets"] is False
    assert body["preferences_updated_at"] is not None

    invalid = await live_client.patch(
        "/api/v1/me/preferences",
        headers=headers,
        json={"preferred_categories": ["Не категория"]},
    )
    assert invalid.status_code == 422

    unauth = await live_client.patch(
        "/api/v1/me/preferences",
        json={"preferred_categories": []},
    )
    assert unauth.status_code == 401
