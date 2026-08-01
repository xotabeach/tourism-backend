"""Ops admin (/admin SQLAdmin) security regressions."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from tourism_backend.config import Settings, validate_settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import create_app
from tourism_backend.modules.admin.application.bootstrap import ensure_bootstrap_admin
from tourism_backend.modules.admin.application.passwords import hash_password, verify_password
from tourism_backend.modules.admin.application.support_ops import operator_reply
from tourism_backend.modules.admin.infrastructure.models import AdminAuditEvent
from tourism_backend.modules.admin.presentation.views import register_views
from tourism_backend.modules.identity.infrastructure.models import AuthOtpChallenge
from tourism_backend.modules.support.infrastructure.models import SupportTicket

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")

_ADMIN_LOGIN = "ops-test"
_ADMIN_PASSWORD = "test-admin-password-ok"


async def _deps_available() -> bool:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.execute(text("SELECT 1 FROM admin_principals LIMIT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
async def admin_client() -> AsyncIterator[AsyncClient]:
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
        auth_otp_store_debug_code=True,
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
        admin_enabled=True,
        admin_session_secret="test-admin-session-secret-32chars!!",
        admin_bootstrap_login=_ADMIN_LOGIN,
        admin_bootstrap_password=_ADMIN_PASSWORD,
    )
    app = create_app(settings)
    await ensure_bootstrap_admin(app.state.session_factory, settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await app.state.redis.aclose()
    await app.state.engine.dispose()


def test_argon2id_password_roundtrip() -> None:
    hashed = hash_password("correct-horse-battery")
    assert hashed.startswith("$argon2id$")
    assert verify_password("correct-horse-battery", hashed)
    assert not verify_password("wrong-password", hashed)


def test_validate_settings_rejects_placeholder_admin_session_in_production() -> None:
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://tourism:prod-db-secret-value@db:5432/tourism",
        database_url_sync="postgresql+psycopg://tourism:prod-db-secret-value@db:5432/tourism",
        redis_url="redis://redis:6379/0",
        jwt_signing_key="production-jwt-signing-key-at-least-32",
        admin_session_secret="local-admin-session-secret-dev-only",
    )
    with pytest.raises(RuntimeError, match="admin session secret"):
        validate_settings(settings)


def test_otp_admin_view_never_lists_code_digest() -> None:
    class _FakeAdmin:
        def __init__(self) -> None:
            self.views: list[object] = []

        def add_view(self, view: object) -> None:
            self.views.append(view)

    admin = _FakeAdmin()
    register_views(admin, Settings(app_env="test", auth_otp_store_debug_code=True))
    otp_view = next(v for v in admin.views if getattr(v, "model", None) is AuthOtpChallenge)
    listed = list(otp_view.column_list)  # type: ignore[attr-defined]
    assert AuthOtpChallenge.code_digest not in listed
    assert AuthOtpChallenge.debug_code in listed
    details_excluded = list(otp_view.column_details_exclude_list)  # type: ignore[attr-defined]
    assert AuthOtpChallenge.code_digest in details_excluded

    admin_prodish = _FakeAdmin()
    register_views(
        admin_prodish,
        Settings(app_env="test", auth_otp_store_debug_code=False),
    )
    otp_hidden = next(
        v for v in admin_prodish.views if getattr(v, "model", None) is AuthOtpChallenge
    )
    assert AuthOtpChallenge.debug_code not in list(otp_hidden.column_list)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_admin_unauthenticated_redirects_to_login() -> None:
    settings = Settings(
        app_env="test",
        admin_enabled=True,
        admin_session_secret="test-admin-session-secret-32chars!!",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/", follow_redirects=False)
    await app.state.redis.aclose()
    await app.state.engine.dispose()
    assert response.status_code in {302, 303}
    assert "/admin/login" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_admin_rejects_mutating_request_without_origin() -> None:
    settings = Settings(
        app_env="test",
        admin_enabled=True,
        admin_session_secret="test-admin-session-secret-32chars!!",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/login",
            data={"username": "x", "password": "y"},
        )
    await app.state.redis.aclose()
    await app.state.engine.dispose()
    assert response.status_code == 403
    assert "CSRF" in response.text


@pytest.mark.asyncio
async def test_admin_login_links_use_forwarded_https() -> None:
    settings = Settings(
        app_env="test",
        admin_enabled=True,
        admin_session_secret="test-admin-session-secret-32chars!!",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
    )
    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/login",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "api.example.test",
            },
        )
    await app.state.redis.aclose()
    await app.state.engine.dispose()
    assert response.status_code == 200
    # ProxyHeadersMiddleware rewrites scheme from X-Forwarded-Proto.
    assert "https://test/admin/statics/" in response.text
    assert 'action="https://test/admin/login"' in response.text
    assert 'href="http://test/admin/statics/' not in response.text
    assert "crimeatrip-admin.css" in response.text
    assert "КРЫМТРИП" in response.text
    assert "ct-login" in response.text


@pytest.mark.asyncio
async def test_mobile_jwt_does_not_authenticate_admin(admin_client: AsyncClient) -> None:
    # Obtain a mobile access token.
    phone = f"+7903{uuid4().int % 10_000_000:07d}"
    req = await admin_client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "Mobile", "phone": phone},
    )
    assert req.status_code == 204, req.text
    verify = await admin_client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": "1234",
            "privacy_accepted": True,
            "personal_data_accepted": True,
        },
    )
    assert verify.status_code == 200, verify.text
    access = verify.json()["access_token"]

    response = await admin_client.get(
        "/admin/user/list",
        headers={"Authorization": f"Bearer {access}"},
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}
    assert "/admin/login" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_admin_login_otp_list_and_operator_reply(admin_client: AsyncClient) -> None:
    headers = {"Origin": "http://test"}
    login = await admin_client.post(
        "/admin/login",
        data={"username": _ADMIN_LOGIN, "password": _ADMIN_PASSWORD},
        headers=headers,
        follow_redirects=False,
    )
    assert login.status_code in {302, 303}, login.text

    otp_list = await admin_client.get("/admin/auth-otp-challenge/list", headers=headers)
    assert otp_list.status_code == 200, otp_list.text
    assert "code_digest" not in otp_list.text

    # Create a support ticket as a mobile user, then reply as operator via service.
    phone = f"+7904{uuid4().int % 10_000_000:07d}"
    await admin_client.post(
        "/api/v1/auth/otp/request",
        json={"display_name": "User", "phone": phone},
    )
    verify = await admin_client.post(
        "/api/v1/auth/otp/verify",
        json={
            "phone": phone,
            "code": "1234",
            "privacy_accepted": True,
            "personal_data_accepted": True,
        },
    )
    access = verify.json()["access_token"]
    ticket_resp = await admin_client.post(
        "/api/v1/support/tickets",
        headers={"Authorization": f"Bearer {access}"},
        json={
            "kind": "chat",
            "subject": "Admin reply test",
            "body": "Need help from ops",
        },
    )
    assert ticket_resp.status_code in {200, 201}, ticket_resp.text
    ticket_id = ticket_resp.json()["id"]

    from uuid import UUID

    from tourism_backend.api.errors import AppError
    from tourism_backend.modules.admin.infrastructure.models import AdminPrincipal

    app = admin_client._transport.app  # type: ignore[attr-defined]
    async with app.state.session_factory() as session:
        principal = (
            await session.execute(
                select(AdminPrincipal).where(AdminPrincipal.login == _ADMIN_LOGIN)
            )
        ).scalar_one()
        with pytest.raises(AppError) as too_long:
            await operator_reply(
                session,
                ticket_id=UUID(ticket_id),
                body="x" * 4001,
                actor_id=principal.id,
            )
        assert too_long.value.status_code == 400

        message = await operator_reply(
            session,
            ticket_id=UUID(ticket_id),
            body="Ops here — we see your request.",
            actor_id=principal.id,
            ip="127.0.0.1",
        )
        assert message.author == "operator"
        audit = (
            await session.execute(
                select(AdminAuditEvent)
                .where(AdminAuditEvent.action == "support.reply")
                .order_by(AdminAuditEvent.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert audit.entity_id == ticket_id

        ticket = await session.get(SupportTicket, UUID(ticket_id))
        assert ticket is not None
        assert ticket.updated_at <= datetime.now(UTC)
