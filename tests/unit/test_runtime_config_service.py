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
    effective_ai_provider_settings,
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
