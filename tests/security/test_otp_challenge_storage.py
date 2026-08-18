"""The readable OTP copy must follow the environment, not the code path.

Runs without Postgres or Redis: the session and Redis client are recorded
in-memory so the persisted challenge can be inspected directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tourism_backend.config import AppEnvironment, Settings
from tourism_backend.modules.identity.application import service
from tourism_backend.modules.identity.application.crypto import digest_matches
from tourism_backend.modules.identity.application.schemas import OtpRequestIn
from tourism_backend.modules.identity.infrastructure.models import AuthOtpChallenge


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _RecordingSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def commit(self) -> None:
        return None

    async def execute(self, statement: Any) -> Any:
        if getattr(statement, "is_select", False):
            now = datetime.now(UTC)
            active = [
                row
                for row in self.added
                if isinstance(row, AuthOtpChallenge)
                and row.consumed_at is None
                and row.expires_at > now
            ]
            active.sort(key=lambda row: row.created_at, reverse=True)
            return _ScalarResult(active[0] if active else None)
        if getattr(statement, "is_update", False):
            keep_id = None
            for row in reversed(self.added):
                if isinstance(row, AuthOtpChallenge) and row.consumed_at is None:
                    keep_id = row.id
                    break
            now = datetime.now(UTC)
            for row in self.added:
                if (
                    isinstance(row, AuthOtpChallenge)
                    and row.consumed_at is None
                    and row.id != keep_id
                ):
                    row.consumed_at = now
            return _ScalarResult(None)
        raise AssertionError(f"unexpected statement: {type(statement)!r}")


class _CountingRedis:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.store: dict[str, str] = {}

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def set(
        self,
        name: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        del ex
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    async def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if self.store.pop(name, None) is not None:
                removed += 1
        return removed


async def _request_otp(settings: Settings) -> AuthOtpChallenge:
    session = _RecordingSession()
    await service.request_otp(
        session,  # type: ignore[arg-type]
        _CountingRedis(),  # type: ignore[arg-type]
        settings,
        OtpRequestIn(display_name="Никита", phone="+79001234567"),
        client_ip="203.0.113.7",
    )
    challenge = session.added[0]
    assert isinstance(challenge, AuthOtpChallenge)
    return challenge


async def test_debug_code_is_stored_and_matches_the_digest_when_enabled() -> None:
    challenge = await _request_otp(Settings(app_env=AppEnvironment.TEST))
    assert challenge.debug_code is not None
    assert challenge.debug_code.isdigit()
    assert digest_matches(challenge.debug_code, challenge.code_digest)


async def test_debug_code_is_absent_when_disabled_but_the_digest_remains() -> None:
    settings = Settings(app_env=AppEnvironment.TEST, auth_otp_store_debug_code=False)
    challenge = await _request_otp(settings)
    assert challenge.debug_code is None
    assert len(challenge.code_digest) == 64


@pytest.mark.parametrize("env", [AppEnvironment.STAGING, AppEnvironment.PRODUCTION])
async def test_deployed_environments_never_persist_a_readable_code(
    env: AppEnvironment,
) -> None:
    challenge = await _request_otp(Settings(app_env=env, jwt_signing_key="x" * 48))
    assert challenge.debug_code is None


async def test_otp_request_is_rate_limited_per_ip_and_phone() -> None:
    """accept-any bypasses the limiter, so a real-code contour must not bypass it."""
    settings = Settings(app_env=AppEnvironment.TEST)
    session = _RecordingSession()
    redis = _CountingRedis()
    payload = OtpRequestIn(display_name="Никита", phone="+79001234567")
    await service.request_otp(
        session,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        settings,
        payload,
        client_ip="203.0.113.7",
    )
    assert redis.counters == {
        "auth:otp:req:ip:203.0.113.7": 1,
        "auth:otp:req:phone:+79001234567": 1,
    }


def _active_otp_rows(session: _RecordingSession) -> list[AuthOtpChallenge]:
    now = datetime.now(UTC)
    return [
        row
        for row in session.added
        if isinstance(row, AuthOtpChallenge) and row.consumed_at is None and row.expires_at > now
    ]


async def test_repeat_otp_request_reuses_the_live_code() -> None:
    settings = Settings(app_env=AppEnvironment.TEST)
    session = _RecordingSession()
    redis = _CountingRedis()
    first = OtpRequestIn(display_name="Никита", phone="+79001234567")
    second = OtpRequestIn(display_name="Никита В.", phone="+79001234567")
    await service.request_otp(
        session,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        settings,
        first,
        client_ip="203.0.113.7",
    )
    await service.request_otp(
        session,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        settings,
        second,
        client_ip="203.0.113.7",
    )
    active = _active_otp_rows(session)
    assert len(active) == 1
    assert active[0].display_name == "Никита В."
    assert active[0].debug_code is not None


async def test_otp_request_skips_issue_while_lock_is_held() -> None:
    settings = Settings(app_env=AppEnvironment.TEST)
    session = _RecordingSession()
    redis = _CountingRedis()
    redis.store["auth:otp:issue:+79001234567"] = "1"
    await service.request_otp(
        session,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        settings,
        OtpRequestIn(display_name="Никита", phone="+79001234567"),
        client_ip="203.0.113.7",
    )
    assert session.added == []
