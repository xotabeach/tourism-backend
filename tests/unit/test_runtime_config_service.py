"""Unit tests for the admin-editable AI-provider runtime override.

Exercises ``effective_ai_provider_settings`` against a fake session, so no
database is required — the risky, novel behaviour here is "read an override
and fall back safely on anything unexpected", not the DB round-trip.
"""

from __future__ import annotations

import pytest

from tourism_backend.config import AIProvider, Settings
from tourism_backend.modules.runtime_config.application.service import (
    AI_PROVIDER_KEY,
    SMS_PROVIDER_KEY,
    SMS_SENDER_KEY,
    SMS_TEMPLATE_KEY,
    effective_ai_provider_settings,
    effective_sms_settings,
    validate_sms_template,
)


class _FakeSetting:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeSession:
    def __init__(self, rows: dict[str, _FakeSetting]) -> None:
        self._rows = rows

    async def get(self, _model: object, key: str) -> _FakeSetting | None:
        return self._rows.get(key)


class _ExplodingSession:
    async def get(self, _model: object, _key: str) -> None:
        raise RuntimeError("db is down")


@pytest.mark.asyncio
async def test_no_override_returns_the_same_settings_object() -> None:
    settings = Settings(ai_provider=AIProvider.MOCK)
    session = _FakeSession(rows={})

    result = await effective_ai_provider_settings(session, settings)  # type: ignore[arg-type]

    assert result is settings


@pytest.mark.asyncio
async def test_valid_override_switches_the_provider() -> None:
    settings = Settings(ai_provider=AIProvider.MOCK)
    session = _FakeSession(rows={AI_PROVIDER_KEY: _FakeSetting("gemini")})

    result = await effective_ai_provider_settings(session, settings)  # type: ignore[arg-type]

    assert result.ai_provider is AIProvider.GEMINI
    # Untouched fields carry over from the original settings unchanged.
    assert result.app_env == settings.app_env


@pytest.mark.asyncio
async def test_unrecognized_stored_value_is_ignored() -> None:
    settings = Settings(ai_provider=AIProvider.MOCK)
    session = _FakeSession(rows={AI_PROVIDER_KEY: _FakeSetting("not-a-real-provider")})

    result = await effective_ai_provider_settings(session, settings)  # type: ignore[arg-type]

    assert result is settings


@pytest.mark.asyncio
async def test_db_failure_falls_back_to_the_static_default() -> None:
    settings = Settings(ai_provider=AIProvider.MOCK)

    result = await effective_ai_provider_settings(_ExplodingSession(), settings)  # type: ignore[arg-type]

    assert result is settings


async def test_sms_runtime_overrides_are_resolved_together() -> None:
    session = _FakeSession(
        rows={
            SMS_PROVIDER_KEY: _FakeSetting("smsaero"),
            SMS_TEMPLATE_KEY: _FakeSetting("Код КрымТрип: {code}"),
            SMS_SENDER_KEY: _FakeSetting("CrimeaTrip"),
        }
    )
    result = await effective_sms_settings(session, Settings())  # type: ignore[arg-type]
    assert result.provider == "smsaero"
    assert result.template == "Код КрымТрип: {code}"
    assert result.sender == "CrimeaTrip"


async def test_invalid_sms_runtime_values_fall_back_safely() -> None:
    settings = Settings()
    session = _FakeSession(
        rows={
            SMS_PROVIDER_KEY: _FakeSetting("unknown"),
            SMS_TEMPLATE_KEY: _FakeSetting("без кода"),
            SMS_SENDER_KEY: _FakeSetting("x"),
        }
    )
    result = await effective_sms_settings(session, settings)  # type: ignore[arg-type]
    assert result.provider == settings.sms_provider
    assert result.template == settings.sms_otp_template
    assert result.sender == settings.sms_sender


def test_sms_template_rejects_unknown_placeholder() -> None:
    with pytest.raises(ValueError, match="только"):
        validate_sms_template("{code}: {name}")


@pytest.mark.parametrize(
    "template",
    ["{code.__class__}", "{code!r}", "{code:>8}", "{code} {code}"],
)
def test_sms_template_rejects_extended_format_syntax(template: str) -> None:
    with pytest.raises(ValueError, match="только"):
        validate_sms_template(template)
