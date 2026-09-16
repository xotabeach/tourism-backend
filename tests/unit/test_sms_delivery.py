"""State-machine tests for durable OTP SMS delivery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest

from tourism_backend.config import Settings
from tourism_backend.modules.identity.application import sms_delivery
from tourism_backend.modules.identity.infrastructure.sms_provider import (
    SmsProviderError,
    SmsSendResult,
    StubSmsProvider,
)
from tourism_backend.modules.runtime_config.application.service import SmsRuntimeSettings


class _Session:
    def __init__(self, job: Any = None, *, scalar_values: list[int] | None = None) -> None:
        self.job = job
        self.commits = 0
        self.executed: list[Any] = []
        self._scalar_values = list(scalar_values or [])

    async def get(self, _model: object, _key: object) -> Any:
        return self.job

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, statement: Any) -> None:
        self.executed.append(statement)

    async def scalar(self, _statement: Any) -> int:
        return self._scalar_values.pop(0)

    async def scalars(self, _statement: Any) -> list[UUID]:
        return [uuid4(), uuid4()]


class _Factory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[_Session]:
        yield self.session


class _Redis:
    def __init__(self) -> None:
        self.count = 0
        self.expiry: int | None = None
        self.locked = False
        self.eval_calls = 0

    async def incr(self, _key: str) -> int:
        self.count += 1
        return self.count

    async def expire(self, _key: str, seconds: int) -> bool:
        self.expiry = seconds
        return True

    async def set(self, _key: str, _value: str, **_kwargs: Any) -> bool:
        if self.locked:
            return False
        self.locked = True
        return True

    async def eval(self, _script: str, _keys: int, _key: str, _token: str) -> int:
        self.eval_calls += 1
        self.locked = False
        return 1


class _Provider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def send(self, **_kwargs: Any) -> SmsSendResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SmsSendResult(provider_sms_id="aero-42")


def _make_job(*, attempts: int = 1, plaintext_code: str | None = "1234") -> Any:
    job = sms_delivery.new_delivery_job(
        phone_e164="+79001234567",
        plaintext_code="1234",
        otp_challenge_id=uuid4(),
    )
    job.attempts = attempts
    job.plaintext_code = plaintext_code
    return job


async def _run_job(monkeypatch: pytest.MonkeyPatch, job: Any, provider: _Provider) -> _Session:
    async def claimed(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def runtime(*_args: Any, **_kwargs: Any) -> SmsRuntimeSettings:
        return SmsRuntimeSettings(
            provider="smsaero", template="Код подтверждения: {code}", sender="КРЫМТРИП"
        )

    async def budget(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sms_delivery, "_claim_job", claimed)
    monkeypatch.setattr(sms_delivery, "effective_sms_settings", runtime)
    monkeypatch.setattr(sms_delivery, "_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(sms_delivery, "_record_daily_send", budget)
    session = _Session(job)
    await sms_delivery.process_job(  # type: ignore[arg-type]
        _Factory(session), _Redis(), Settings(), job.id
    )
    return session


async def test_success_marks_sent_and_erases_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _make_job()
    provider = _Provider()
    session = await _run_job(monkeypatch, job, provider)
    assert provider.calls == 1
    assert job.status == "sent"
    assert job.provider_sms_id == "aero-42"
    assert job.plaintext_code is None
    assert session.commits == 1


async def test_definite_failure_is_scheduled_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _make_job(attempts=1)
    await _run_job(monkeypatch, job, _Provider(SmsProviderError("rejected")))
    assert job.status == "pending"
    assert job.next_attempt_at is not None
    assert job.plaintext_code == "1234"


async def test_ambiguous_failure_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _make_job(attempts=1)
    await _run_job(
        monkeypatch,
        job,
        _Provider(SmsProviderError("timeout", ambiguous=True)),
    )
    assert job.status == "failed"
    assert job.next_attempt_at is None
    assert job.plaintext_code is None


async def test_last_definite_attempt_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _make_job(attempts=3)
    await _run_job(monkeypatch, job, _Provider(SmsProviderError("rejected")))
    assert job.status == "failed"
    assert job.plaintext_code is None


async def test_missing_plaintext_fails_corrupt_job(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _make_job(plaintext_code=None)

    async def claimed(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(sms_delivery, "_claim_job", claimed)
    session = _Session(job)
    await sms_delivery.process_job(  # type: ignore[arg-type]
        _Factory(session), _Redis(), Settings(), job.id
    )
    assert job.status == "failed"
    assert "no plaintext" in job.last_error


async def test_unclaimed_job_is_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def claimed(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(sms_delivery, "_claim_job", claimed)
    session = _Session(_make_job())
    await sms_delivery.process_job(  # type: ignore[arg-type]
        _Factory(session), _Redis(), Settings(), uuid4()
    )
    assert session.commits == 0


async def test_daily_budget_counter_expires_at_next_midnight() -> None:
    redis = _Redis()
    await sms_delivery._record_daily_send(redis, budget=10)  # type: ignore[arg-type]
    assert redis.count == 1
    assert redis.expiry is not None
    assert 0 < redis.expiry <= 86_400


async def test_sweep_uses_owner_token_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def recover(_session: Any) -> None:
        calls.append("recover")

    async def eligible(_session: Any, *, max_attempts: int) -> list[UUID]:
        assert max_attempts == 3
        calls.append("eligible")
        return []

    async def warn(_session: Any) -> None:
        calls.append("warn")

    monkeypatch.setattr(sms_delivery, "_recover_stale_claims", recover)
    monkeypatch.setattr(sms_delivery, "_eligible_job_ids", eligible)
    monkeypatch.setattr(sms_delivery, "_warn_on_failure_ratio", warn)
    redis = _Redis()
    await sms_delivery.sweep_once(  # type: ignore[arg-type]
        _Factory(_Session()), redis, Settings()
    )
    assert calls == ["recover", "eligible", "warn"]
    assert redis.eval_calls == 1


def test_provider_factory_defaults_to_stub_and_requires_credentials() -> None:
    assert isinstance(sms_delivery._provider(Settings(), "stub"), StubSmsProvider)
    with pytest.raises(SmsProviderError, match="credentials"):
        sms_delivery._provider(Settings(), "smsaero")
    with pytest.raises(SmsProviderError, match="unsupported"):
        sms_delivery._provider(Settings(), "other")
