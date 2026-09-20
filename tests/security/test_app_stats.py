"""APK download counter and app-version statistics against Postgres and Redis."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from tourism_backend.config import Settings
from tourism_backend.db.redis import create_redis_client
from tourism_backend.main import _MEDIA_DIR, create_app
from tourism_backend.modules.app_stats.application.apk_downloads import (
    ManifestReader,
    apk_version_for,
    is_bot,
)
from tourism_backend.modules.app_stats.application.app_versions import (
    parse_app_version,
    parse_platform,
)
from tourism_backend.modules.app_stats.application.common import moscow_today
from tourism_backend.modules.app_stats.application.queries import build_report, normalize_period
from tourism_backend.modules.app_stats.application.retention import (
    aggregate_complete_days,
    purge_user_rows,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")
BROWSER = "Mozilla/5.0 (Linux; Android 14) Chrome/120 Mobile Safari/537.36"
PATH = "/media/app/crimeatrip-latest.apk"


def test_parse_app_version_accepts_only_the_strict_format() -> None:
    assert parse_app_version("0.2.6+12") == ("0.2.6", 12)
    assert parse_app_version("0.10.0+120") == ("0.10.0", 120)
    for bad in (None, "", "0.2.6", "v0.2.6+1", "0.2.6+1; drop", "1.2.3+" + "9" * 40, "a.b.c+1"):
        assert parse_app_version(bad) == ("unknown", 0)


def test_parse_platform() -> None:
    assert parse_platform("Android") == "android"
    assert parse_platform("ios") == "ios"
    assert parse_platform("windows") == "unknown"
    assert parse_platform(None) == "unknown"


def test_bot_detection() -> None:
    assert is_bot("")
    assert is_bot("TelegramBot (like TwitterBot)")
    assert is_bot("Mozilla/5.0 (compatible; YandexBot/3.0)")
    assert is_bot("facebookexternalhit/1.1")
    assert not is_bot(BROWSER)


def test_apk_version_from_name_and_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "latest.json"
    reader = ManifestReader(manifest_path)
    assert apk_version_for("/media/app/crimeatrip-latest.apk", reader) == "latest"
    manifest_path.write_text(json.dumps({"version": "0.2.6+12"}))
    assert apk_version_for("/media/app/crimeatrip-latest.apk", reader) == "0.2.6+12"
    assert apk_version_for("/media/app/crimeatrip-0.2.5.apk", reader) == "0.2.5"
    assert apk_version_for("/media/app/whatever.apk", reader) == "other"
    manifest_path.write_text(json.dumps({"version": "x; DROP"}))
    os.utime(manifest_path, (1, 1_900_000_000))
    assert apk_version_for("/media/app/crimeatrip-latest.apk", reader) == "latest"


def test_normalize_period() -> None:
    assert normalize_period("7") == 7
    assert normalize_period("90") == 90
    assert normalize_period("13") == 30
    assert normalize_period("abc") == 30
    assert normalize_period(None) == 30


async def _deps_available() -> bool:
    try:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await engine.dispose()
        redis = create_redis_client(Settings(redis_url=REDIS_URL))
        await redis.ping()
        await redis.aclose()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture
def apk_file() -> Iterator[Path]:
    app_dir = _MEDIA_DIR / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    apk = app_dir / "crimeatrip-latest.apk"
    manifest = app_dir / "latest.json"
    had_apk = apk.exists()
    had_manifest = manifest.exists()
    if not had_apk:
        apk.write_bytes(b"PK" + b"0" * 4096)
    if not had_manifest:
        manifest.write_text(json.dumps({"version": "9.9.9+999"}))
    yield apk
    if not had_apk:
        apk.unlink(missing_ok=True)
    if not had_manifest:
        manifest.unlink(missing_ok=True)


@pytest.fixture
async def live(apk_file: Path) -> AsyncIterator[tuple[AsyncClient, AsyncEngine, object]]:
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
        apk_stats_salt="test-salt",
    )
    app = create_app(settings)
    engine = create_async_engine(DATABASE_URL)
    async for key in app.state.redis.scan_iter("apkdl:*"):
        await app.state.redis.delete(key)
    async for key in app.state.redis.scan_iter("appver:*"):
        await app.state.redis.delete(key)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM apk_download_daily"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, engine, app
    await engine.dispose()
    await app.state.redis.aclose()
    await app.state.engine.dispose()


async def _downloads(engine: AsyncEngine, *, source: str | None = None) -> int:
    sql = "SELECT COALESCE(SUM(count), 0) FROM apk_download_daily"
    params: dict[str, str] = {}
    if source:
        sql += " WHERE source = :s"
        params["s"] = source
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar() or 0)


async def _settle(engine: AsyncEngine, expected: int, **kw: str) -> int:
    value = 0
    for _ in range(40):
        value = await _downloads(engine, **kw)
        if value >= expected:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.15)  # would-be extra counts land here
    return await _downloads(engine, **kw)


async def test_download_counted_once_within_dedupe_window(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, _ = live
    headers = {"User-Agent": BROWSER}
    first = await client.get(PATH, headers=headers)
    assert first.status_code == 200
    assert len(first.content) > 1000  # the whole file is served
    await client.get(PATH, headers=headers)
    assert await _settle(engine, 1) == 1


async def test_range_requests_do_not_double_count(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, _ = live
    headers = {"User-Agent": BROWSER + " r", "Range": "bytes=0-99"}
    responses = await asyncio.gather(*(client.get(PATH, headers=headers) for _ in range(3)))
    assert all(r.status_code == 206 for r in responses)
    assert await _settle(engine, 1) == 1


async def test_head_bots_304_and_416_are_not_counted(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, _ = live
    assert (await client.head(PATH, headers={"User-Agent": BROWSER + " h"})).status_code == 200
    assert (await client.get(PATH, headers={"User-Agent": "TelegramBot"})).status_code == 200
    bad = await client.get(PATH, headers={"User-Agent": BROWSER + " x", "Range": "bytes=999999-"})
    assert bad.status_code == 416
    ok = await client.get(PATH, headers={"User-Agent": BROWSER + " y"})
    etag = ok.headers["etag"]
    await asyncio.sleep(0.3)
    baseline = await _downloads(engine)
    cached = await client.get(PATH, headers={"User-Agent": BROWSER + " z", "If-None-Match": etag})
    assert cached.status_code == 304
    assert await _settle(engine, baseline) == baseline == 1


async def test_source_comes_from_caddy_header_and_defaults_to_direct(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, _ = live
    await client.get(PATH, headers={"User-Agent": BROWSER + " a", "X-Download-Source": "landing"})
    await client.get(PATH, headers={"User-Agent": BROWSER + " b", "X-Download-Source": "hacked"})
    await client.get(PATH, headers={"User-Agent": BROWSER + " c"})
    await _settle(engine, 3)
    assert await _downloads(engine, source="landing") == 1
    assert await _downloads(engine, source="direct") == 2


async def test_version_taken_from_manifest_and_file_name(live, apk_file: Path) -> None:  # type: ignore[no-untyped-def]
    client, engine, _ = live
    versioned = apk_file.parent / "crimeatrip-0.0.1.apk"
    versioned.write_bytes(b"PK" + b"1" * 100)
    try:
        await client.get("/media/app/crimeatrip-0.0.1.apk", headers={"User-Agent": BROWSER})
        await _settle(engine, 1)
    finally:
        versioned.unlink(missing_ok=True)
    async with engine.connect() as conn:
        versions = {
            r[0] for r in (await conn.execute(text("SELECT apk_version FROM apk_download_daily")))
        }
    assert versions == {"0.0.1"}


async def test_hourly_limit_per_address(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, _ = live
    for i in range(8):
        await client.get(PATH, headers={"User-Agent": f"{BROWSER} n{i}"})
    assert await _settle(engine, 5) == 5


async def test_redis_failure_still_serves_and_counts(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, app = live

    class BrokenRedis:
        async def set(self, *a: object, **k: object) -> None:
            raise ConnectionError("redis down")

    real = app.state.redis
    app.state.redis = BrokenRedis()
    try:
        response = await client.get(PATH, headers={"User-Agent": BROWSER + " rf"})
        assert response.status_code == 200
        assert await _settle(engine, 1) == 1
    finally:
        app.state.redis = real


async def test_disabled_flag_counts_nothing(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, app = live
    app.state.settings = app.state.settings.model_copy(update={"apk_stats_enabled": False})
    response = await client.get(PATH, headers={"User-Agent": BROWSER + " off"})
    assert response.status_code == 200
    await asyncio.sleep(0.3)
    assert await _downloads(engine) == 0


async def _login(client: AsyncClient, headers: dict[str, str]) -> tuple[dict[str, str], UUID]:
    phone = f"+7902{uuid4().int % 10_000_000:07d}"
    requested = await client.post(
        "/api/v1/auth/otp/request", json={"display_name": "Версия", "phone": phone}
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
    auth = {"Authorization": f"Bearer {verified.json()['access_token']}", **headers}
    me = await client.get("/api/v1/me", headers=auth)
    assert me.status_code == 200, me.text
    return auth, UUID(me.json()["id"])


async def _user_rows(engine: AsyncEngine, user_id: UUID) -> set[tuple[str, int, str]]:
    for _ in range(40):
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT app_version, build_number, platform "
                        "FROM app_version_daily_users WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )
            ).all()
        if rows:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.15)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT app_version, build_number, platform "
                    "FROM app_version_daily_users WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).all()
    return {(r[0], r[1], r[2]) for r in rows}


async def test_authenticated_request_records_version_once_per_day(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, _ = live
    hdr = {"X-App-Version": "0.2.6+12", "X-App-Platform": "android"}
    auth, user_id = await _login(client, hdr)
    for _ in range(3):
        assert (await client.get("/api/v1/me", headers=auth)).status_code == 200
    assert await _user_rows(engine, user_id) == {("0.2.6", 12, "android")}
    # A second version for the same user counts in both.
    older = {**auth, "X-App-Version": "0.2.5+9"}
    await client.get("/api/v1/me", headers=older)
    await asyncio.sleep(0.4)
    assert await _user_rows(engine, user_id) == {("0.2.6", 12, "android"), ("0.2.5", 9, "android")}


async def test_missing_or_malformed_header_is_unknown(live) -> None:  # type: ignore[no-untyped-def]
    client, engine, _ = live
    auth, user_id = await _login(client, {"X-App-Version": "'; DROP TABLE users;--"})
    assert (await client.get("/api/v1/me", headers=auth)).status_code == 200
    assert await _user_rows(engine, user_id) == {("unknown", 0, "unknown")}


async def test_report_share_counts_unknown_in_denominator_and_compares_builds(live) -> None:  # type: ignore[no-untyped-def]
    _, engine, _ = live
    day = moscow_today() - timedelta(days=1)
    users = [uuid4() for _ in range(4)]
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM app_version_daily_users WHERE day = :d"), {"d": day})
        for i, uid in enumerate(users):
            await conn.execute(
                text(
                    "INSERT INTO users (id, display_name, phone_e164, created_at, updated_at) "
                    "VALUES (:id, 'Т', :p, now(), now())"
                ),
                {"id": uid, "p": f"+7903{i:07d}{uuid4().int % 10}"[:12]},
            )
        rows = [
            (users[0], "0.10.0", 120),  # 0.10.0 must outrank 0.9.0
            (users[1], "0.9.0", 90),
            (users[2], "unknown", 0),
            (users[3], "0.10.0", 120),
        ]
        for uid, version, build in rows:
            await conn.execute(
                text("INSERT INTO app_version_daily_users VALUES (:u, :d, :v, :b, 'android')"),
                {"u": uid, "d": day, "v": version, "b": build},
            )
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as session:
            report = await build_report(session, today=moscow_today(), days=7, new_from_build=100)
        row = next(r for r in report.share_series if r.day == day)
        assert (row.total, row.fresh, row.percent) == (4, 2, 50)
        assert report.share_day == day
        # build 0 (unknown) never counts as new, even with threshold 0
        async with sessions() as session:
            zero = await build_report(session, today=moscow_today(), days=7, new_from_build=0)
        assert next(r for r in zero.share_series if r.day == day).fresh == 3
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM app_version_daily_users WHERE day = :d"), {"d": day}
            )
            await conn.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": users})


def test_aggregate_then_purge_keeps_history() -> None:
    from sqlalchemy import create_engine

    sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg")
    try:
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        pytest.skip("Postgres for integration tests is unavailable")
    today = moscow_today()
    old_day = today - timedelta(days=40)
    fresh_day = today - timedelta(days=2)
    uid = uuid4()
    with Session(engine) as session:
        session.execute(
            text(
                "INSERT INTO users (id, display_name, phone_e164, created_at, updated_at) "
                "VALUES (:id, 'Т', :p, now(), now())"
            ),
            {"id": uid, "p": f"+7904{uuid4().int % 10_000_000:07d}"},
        )
        for d in (old_day, fresh_day):
            session.execute(
                text("INSERT INTO app_version_daily_users VALUES (:u, :d, '1.0.0', 5, 'ios')"),
                {"u": uid, "d": d},
            )
        session.commit()
        try:
            assert purge_user_rows(session, today=today, apply=False) >= 1  # dry-run
            aggregate_complete_days(session, today=today)
            assert purge_user_rows(session, today=today, apply=True) >= 1
            session.commit()
            left = session.execute(
                text("SELECT day FROM app_version_daily_users WHERE user_id = :u"), {"u": uid}
            ).all()
            assert [r[0] for r in left] == [fresh_day]
            kept = session.execute(
                text(
                    "SELECT users FROM app_version_daily WHERE day = :d AND app_version = '1.0.0'"
                ),
                {"d": old_day},
            ).scalar()
            assert kept == 1
        finally:
            session.execute(text("DELETE FROM app_version_daily WHERE app_version = '1.0.0'"))
            session.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            session.commit()
