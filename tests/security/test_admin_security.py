"""Ops admin (/admin SQLAdmin) security regressions."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
from tourism_backend.modules.notifications.infrastructure.models import Notification
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
    assert otp_view.column_default_sort == (AuthOtpChallenge.created_at, True)  # type: ignore[attr-defined]
    filter_names = {getattr(f, "parameter_name", None) for f in otp_view.column_filters}  # type: ignore[attr-defined]
    assert "phone_e164" in filter_names
    assert "user_id" in filter_names

    admin_prodish = _FakeAdmin()
    register_views(
        admin_prodish,
        Settings(app_env="test", auth_otp_store_debug_code=False),
    )
    otp_hidden = next(
        v for v in admin_prodish.views if getattr(v, "model", None) is AuthOtpChallenge
    )
    assert AuthOtpChallenge.debug_code not in list(otp_hidden.column_list)  # type: ignore[attr-defined]


def test_user_admin_allows_edit_and_shows_media_formatters() -> None:
    from tourism_backend.modules.admin.presentation.views import UserAdmin
    from tourism_backend.modules.identity.infrastructure.models import User

    assert UserAdmin.can_edit is True
    assert User.display_name in UserAdmin.column_formatters
    assert User.id in UserAdmin.column_formatters
    assert User.travel_points not in UserAdmin.form_columns
    assert User.travel_points in UserAdmin.column_list


def test_support_ticket_admin_sorts_and_flags_awaiting_reply() -> None:
    from tourism_backend.modules.admin.presentation.filters import AwaitingOperatorReplyFilter
    from tourism_backend.modules.admin.presentation.formatters import format_ticket_awaiting
    from tourism_backend.modules.admin.presentation.views import SupportTicketAdmin
    from tourism_backend.modules.support.infrastructure.models import SupportTicket

    assert SupportTicket.last_message_at in SupportTicketAdmin.column_sortable_list
    assert SupportTicketAdmin.column_default_sort == (SupportTicket.last_message_at, True)
    assert any(
        isinstance(f, AwaitingOperatorReplyFilter) for f in SupportTicketAdmin.column_filters
    )

    awaiting = format_ticket_awaiting(
        __import__("types").SimpleNamespace(status="open", last_human_author="user"),
        None,
    )
    assert "Ждёт ответа" in str(awaiting)
    assert "ct-ticket-awaiting" in str(awaiting)
    answered = format_ticket_awaiting(
        __import__("types").SimpleNamespace(status="open", last_human_author="operator"),
        None,
    )
    assert "Отвечено" in str(answered)


def test_admin_moscow_datetime_formatter() -> None:
    from datetime import UTC, datetime

    from tourism_backend.modules.admin.presentation.datetime_fmt import (
        format_moscow_datetime,
        format_moscow_plain,
        to_moscow,
    )

    utc = datetime(2026, 8, 3, 10, 0, 0, 123456, tzinfo=UTC)
    msk = to_moscow(utc)
    assert msk.hour == 13  # UTC+3
    assert msk.microsecond == 0
    rendered = str(format_moscow_datetime(utc))
    assert "МСК" in rendered
    assert "2026-08-03 13:00:00" in rendered
    assert ".123456" not in rendered
    assert format_moscow_plain(utc) == "2026-08-03 13:00:00 МСК"


def test_support_ticket_chat_reply_is_exposed_not_model_create() -> None:
    from pathlib import Path

    from tourism_backend.modules.admin.presentation.views import SupportTicketAdmin

    reply = SupportTicketAdmin.post_reply
    assert getattr(reply, "_exposed", False) is True
    assert getattr(reply, "_path", None) == "/reply/{pk}"
    assert "POST" in getattr(reply, "_methods", [])

    messages = SupportTicketAdmin.list_messages
    assert getattr(messages, "_exposed", False) is True
    assert getattr(messages, "_path", None) == "/messages/{pk}"
    assert "GET" in getattr(messages, "_methods", [])

    template = (
        Path(__file__).resolve().parents[2]
        / "src/tourism_backend/modules/admin/theme/templates/sqladmin/support_chat.html"
    ).read_text(encoding="utf-8")
    assert "view-support-ticket-post_reply" in template
    assert "view-support-ticket-list_messages" in template
    assert "setInterval" in template
    assert "escapeHtml" in template
    assert "admin:create" not in template
    assert "support-message" not in template


def test_media_formatter_rejects_unsafe_urls() -> None:
    from tourism_backend.modules.admin.presentation.formatters import _safe_media_url

    assert _safe_media_url("/media/profiles/x.webp") == "/media/profiles/x.webp"
    assert _safe_media_url("javascript:alert(1)") is None
    assert _safe_media_url("/media/../etc/passwd") is None
    assert _safe_media_url("https://evil.example/a.png") is None


def test_admin_formatters_escape_and_render_media() -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from starlette.requests import Request

    from tourism_backend.modules.admin.presentation.formatters import (
        format_admin_role,
        format_debug_code,
        format_message_author,
        format_ticket_kind,
        format_ticket_status,
        format_user_avatar_name,
        format_user_cover,
        format_user_id_peek,
    )

    assert "Открыт" in str(format_ticket_status(SimpleNamespace(status="open"), None))
    assert "Чат" in str(format_ticket_kind(SimpleNamespace(kind="chat"), None))
    assert "Оператор" in str(format_message_author(SimpleNamespace(author="operator"), None))
    assert "Admin" in str(format_admin_role(SimpleNamespace(role="admin"), None))
    assert "—" in str(format_debug_code(SimpleNamespace(debug_code=None), None))
    assert "1234" in str(format_debug_code(SimpleNamespace(debug_code="1234"), None))

    uid = uuid4()
    peek = str(format_user_id_peek(SimpleNamespace(user_id=uid), None))
    assert str(uid) in peek
    assert "ct-user-peek" in peek or "ct-entity-link" in peek
    assert "—" in str(format_user_id_peek(SimpleNamespace(user_id=None), None))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    request = Request(scope)
    request.state.user_media = {
        uid: {"avatar": "/media/a.webp", "cover": "/media/c.webp"},
    }
    user = SimpleNamespace(id=uid, display_name="Ada")
    avatar_html = str(format_user_avatar_name(user, "display_name", request))
    assert "/media/a.webp" in avatar_html
    assert "Ada" in avatar_html
    cover_html = str(format_user_cover(user, "id", request))
    assert "/media/c.webp" in cover_html

    empty_req = Request(scope)
    empty_req.state.user_media = {}
    assert "ct-user-avatar-fallback" in str(
        format_user_avatar_name(user, "display_name", empty_req)
    )
    assert "нет баннера" in str(format_user_cover(user, "id", empty_req))


@pytest.mark.asyncio
async def test_otp_linked_user_id_filter_builds_query() -> None:
    from uuid import uuid4

    from sqlalchemy import select

    from tourism_backend.modules.admin.presentation.filters import OtpLinkedUserIdFilter
    from tourism_backend.modules.identity.infrastructure.models import AuthOtpChallenge

    filt = OtpLinkedUserIdFilter()
    base = select(AuthOtpChallenge)
    assert await filt.get_filtered_query(base, "equals", "", AuthOtpChallenge) is base
    bad = await filt.get_filtered_query(base, "equals", "not-a-uuid", AuthOtpChallenge)
    assert bad is not base
    uid = uuid4()
    ok = await filt.get_filtered_query(base, "equals", str(uid), AuthOtpChallenge)
    compiled = str(ok.compile(compile_kwargs={"literal_binds": False}))
    assert "phone_e164" in compiled.lower() or "users" in compiled.lower()
    contains = await filt.get_filtered_query(base, "contains", str(uid)[:8], AuthOtpChallenge)
    assert contains is not base
    starts = await filt.get_filtered_query(base, "starts_with", str(uid)[:4], AuthOtpChallenge)
    assert starts is not base
    assert filt.get_operation_options_for_model(AuthOtpChallenge)
    assert await filt.lookups(None, AuthOtpChallenge, None) == []  # type: ignore[arg-type]
    assert await filt.get_filtered_query(base, "unknown", "x", AuthOtpChallenge) is base
    assert "—" in str(
        __import__(
            "tourism_backend.modules.admin.presentation.formatters",
            fromlist=["format_user_cover"],
        ).format_user_cover(__import__("types").SimpleNamespace(id="nope"), "id", None)
    )


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
async def test_admin_rejects_cross_site_fetch_even_with_matching_host_hint() -> None:
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
            headers={"Sec-Fetch-Site": "cross-site"},
        )
    await app.state.redis.aclose()
    await app.state.engine.dispose()
    assert response.status_code == 403
    assert "CSRF" in response.text


@pytest.mark.asyncio
async def test_admin_accepts_same_origin_sec_fetch_without_origin(
    admin_client: AsyncClient,
) -> None:
    """Browsers behind Referrer-Policy: no-referrer may omit Origin/Referer."""
    response = await admin_client.post(
        "/admin/login",
        data={"username": "x", "password": "y"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    # CSRF passed; login itself fails with bad credentials (400 from SQLAdmin).
    assert response.status_code != 403
    assert "CSRF" not in response.text


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
    assert 'action="/admin/login"' in response.text
    assert 'href="http://test/admin/statics/' not in response.text
    assert 'name="referrer" content="same-origin"' in response.text
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
    otp_filtered = await admin_client.get(
        "/admin/auth-otp-challenge/list",
        params={"phone_e164": "+7", "phone_e164_op": "contains"},
        headers=headers,
    )
    assert otp_filtered.status_code == 200, otp_filtered.text

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
    me = await admin_client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert me.status_code == 200, me.text
    user_id = me.json()["id"]

    users_list = await admin_client.get("/admin/user/list", headers=headers)
    assert users_list.status_code == 200, users_list.text
    assert "ct-user-profile-cell" in users_list.text or "User" in users_list.text

    user_details = await admin_client.get(f"/admin/user/details/{user_id}", headers=headers)
    assert user_details.status_code == 200, user_details.text

    # travel_points must remain auto-managed (posted value ignored).
    from tourism_backend.modules.identity.infrastructure.models import User

    app = admin_client._transport.app  # type: ignore[attr-defined]
    async with app.state.session_factory() as session:
        before = await session.get(User, UUID(user_id))
        assert before is not None
        points_before = before.travel_points

    edit = await admin_client.post(
        f"/admin/user/edit/{user_id}",
        data={
            "display_name": "User Edited",
            "phone_e164": phone,
            "travel_points": "999999",
            "notify_push_enabled": "y",
            "notify_sms_enabled": "",
            "notify_haptics_enabled": "y",
        },
        headers=headers,
        follow_redirects=False,
    )
    assert edit.status_code in {302, 303, 200}, edit.text

    async with app.state.session_factory() as session:
        after = await session.get(User, UUID(user_id))
        assert after is not None
        assert after.display_name == "User Edited"
        assert after.travel_points == points_before

    user_edit_page = await admin_client.get(f"/admin/user/edit/{user_id}", headers=headers)
    assert user_edit_page.status_code == 200, user_edit_page.text
    assert "avatar_file" in user_edit_page.text
    assert "cover_file" in user_edit_page.text
    assert 'name="travel_points"' not in user_edit_page.text

    tickets_list = await admin_client.get("/admin/support-ticket/list", headers=headers)
    assert tickets_list.status_code == 200, tickets_list.text

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

    awaiting_list = await admin_client.get(
        "/admin/support-ticket/list",
        params={"awaiting_reply": "1"},
        headers=headers,
    )
    assert awaiting_list.status_code == 200, awaiting_list.text
    assert "Ждёт ответа" in awaiting_list.text or ticket_id[:8] in awaiting_list.text
    assert "МСК" in awaiting_list.text

    from tourism_backend.api.errors import AppError
    from tourism_backend.modules.admin.infrastructure.models import AdminPrincipal

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
        notification = (
            await session.execute(
                select(Notification)
                .where(
                    Notification.kind == "support_reply",
                    Notification.target_id == UUID(ticket_id),
                )
                .order_by(Notification.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert notification.target_type == "support_ticket"
        assert notification.is_read is False

        ticket = await session.get(SupportTicket, UUID(ticket_id))
        assert ticket is not None
        assert ticket.updated_at <= datetime.now(UTC)

    chat = await admin_client.get(
        f"/admin/support-ticket/details/{ticket_id}",
        headers=headers,
    )
    assert chat.status_code == 200, chat.text
    assert "ct-chat-shell" in chat.text
    assert "Need help from ops" in chat.text
    assert "Ops here" in chat.text
    assert f"/admin/support-ticket/reply/{ticket_id}" in chat.text
    assert "/admin/support-message/create" not in chat.text

    # Compose box posts into the ticket chat route (not model create).
    compose = await admin_client.post(
        f"/admin/support-ticket/reply/{ticket_id}",
        data={"body": "Chat compose reply from ops"},
        headers=headers,
        follow_redirects=False,
    )
    assert compose.status_code in {302, 303}, compose.text
    assert f"/admin/support-ticket/details/{ticket_id}" in compose.headers.get("location", "")

    chat_after = await admin_client.get(
        f"/admin/support-ticket/details/{ticket_id}",
        headers=headers,
    )
    assert chat_after.status_code == 200, chat_after.text
    assert "Chat compose reply from ops" in chat_after.text
    assert f"/admin/support-ticket/messages/{ticket_id}" in chat_after.text
    assert "ct-chat-live" in chat_after.text

    live = await admin_client.get(
        f"/admin/support-ticket/messages/{ticket_id}",
        headers={**headers, "Accept": "application/json"},
    )
    assert live.status_code == 200, live.text
    payload = live.json()
    assert payload["ticket_id"] == ticket_id
    assert payload["status"] == "open"
    bodies = [m["body"] for m in payload["messages"]]
    assert "Need help from ops" in bodies
    assert "Chat compose reply from ops" in bodies
    assert all("id" in m and "author_key" in m for m in payload["messages"])
    # Poll payload is data, not HTML — bodies stay plain text.
    assert "<script>" not in live.text
