"""The app's error reports (BACKEND-73, spec 47)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
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


async def _user(client: AsyncClient, name: str) -> tuple[str, dict[str, str]]:
    tokens = await _login(client, phone=f"+7906{uuid4().int % 10_000_000:07d}", name=name)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await client.get("/api/v1/me", headers=headers)
    return me.json()["id"], headers


async def _sql(statement: str, **params: object) -> None:
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)
    await engine.dispose()


def _report(**over: object) -> dict:
    report = {
        "kind": "crash",
        "error_type": "StateError",
        "stack": "#0 main (package:tourism_mobile/main.dart:10)",
        "screen": "/routes/abc?tab=reviews",
        "app_version": "0.3.6",
        "build_number": 34,
        "platform": "android",
        "device_model": "Pixel 7",
        "os_version": "14",
        "occurred_at": "2026-10-15T10:00:00Z",
    }
    report.update(over)
    return report


async def _sql_one(statement: str, **params: object) -> object:
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as conn:
        value = (await conn.execute(text(statement), params)).scalar()
    await engine.dispose()
    return value


@pytest.mark.asyncio
async def test_only_signed_in_people_report(live_client: AsyncClient) -> None:
    anon = await live_client.post("/api/v1/app/errors", json={"reports": [_report()]})
    assert anon.status_code == 401


@pytest.mark.asyncio
async def test_reports_are_kept_without_query_strings(live_client: AsyncClient) -> None:
    anna, anna_h = await _user(live_client, "Анна")
    sent = await live_client.post(
        "/api/v1/app/errors",
        headers=anna_h,
        json={"reports": [_report(), _report(kind="blank_screen", screen="/home")]},
    )
    assert sent.status_code == 202, sent.text
    assert sent.json() == {"accepted": 2}
    screens = await _sql_one(
        "SELECT string_agg(screen, ',' ORDER BY screen) FROM app_error_reports WHERE user_id = :id",
        id=anna,
    )
    assert screens == "/home,/routes/abc"


@pytest.mark.asyncio
async def test_nothing_beyond_the_known_fields_is_taken(live_client: AsyncClient) -> None:
    _anna, anna_h = await _user(live_client, "Анна")
    # The error's own text may carry what the person typed: not accepted.
    extra = await live_client.post(
        "/api/v1/app/errors",
        headers=anna_h,
        json={"reports": [_report(message="телефон +79001234567")]},
    )
    assert extra.status_code == 422
    unknown = await live_client.post(
        "/api/v1/app/errors", headers=anna_h, json={"reports": [_report(kind="oops")]}
    )
    assert unknown.status_code == 422
    too_many = await live_client.post(
        "/api/v1/app/errors", headers=anna_h, json={"reports": [_report()] * 21}
    )
    assert too_many.status_code == 422


@pytest.mark.asyncio
async def test_a_big_request_is_refused(live_client: AsyncClient) -> None:
    _anna, anna_h = await _user(live_client, "Анна")
    big = await live_client.post(
        "/api/v1/app/errors",
        headers=anna_h,
        json={"reports": [_report(stack="x" * 8000) for _ in range(10)]},
    )
    assert big.status_code == 413


@pytest.mark.asyncio
async def test_twenty_reports_an_hour(live_client: AsyncClient) -> None:
    _anna, anna_h = await _user(live_client, "Анна")
    first = await live_client.post(
        "/api/v1/app/errors", headers=anna_h, json={"reports": [_report()] * 20}
    )
    assert first.status_code == 202, first.text
    more = await live_client.post(
        "/api/v1/app/errors", headers=anna_h, json={"reports": [_report()]}
    )
    assert more.status_code == 429


@pytest.mark.asyncio
async def test_reports_go_with_the_account_and_after_thirty_days(
    live_client: AsyncClient,
) -> None:
    from datetime import date

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from tourism_backend.modules.app_stats.application.error_reports import purge_reports

    anna, anna_h = await _user(live_client, "Анна")
    sent = await live_client.post(
        "/api/v1/app/errors", headers=anna_h, json={"reports": [_report(), _report()]}
    )
    assert sent.status_code == 202
    await _sql(
        "UPDATE app_error_reports SET created_at = now() - interval '40 days'"
        " WHERE user_id = :id AND id = (SELECT id FROM app_error_reports"
        " WHERE user_id = :id LIMIT 1)",
        id=anna,
    )
    engine = create_engine(DATABASE_URL.replace("+asyncpg", "+psycopg"))
    with Session(engine) as session:
        assert purge_reports(session, today=date.today(), apply=True) >= 1
        session.commit()
    engine.dispose()
    left = await _sql_one("SELECT count(*) FROM app_error_reports WHERE user_id = :id", id=anna)
    assert left == 1
    await _sql("DELETE FROM users WHERE id = :id", id=anna)
    gone = await _sql_one("SELECT count(*) FROM app_error_reports WHERE user_id = :id", id=anna)
    assert gone == 0
