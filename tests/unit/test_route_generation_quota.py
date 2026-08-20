"""Unit tests for generation quota helpers."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tourism_backend.api.errors import AppError
from tourism_backend.modules.route_builder.application.quota import (
    _start_of_utc_day,
    _start_of_utc_week,
)
from tourism_backend.modules.subscriptions.application.entitlements import (
    FREE_POLICY,
    TRAVEL_PLUS_POLICY,
)


def test_period_boundaries_are_utc() -> None:
    now = datetime(2026, 8, 20, 15, 30, tzinfo=UTC)
    assert _start_of_utc_day(now) == datetime(2026, 8, 20, tzinfo=UTC)
    assert _start_of_utc_week(now) == datetime(2026, 8, 17, tzinfo=UTC)


def test_policies_expose_generation_caps() -> None:
    assert FREE_POLICY.max_weekly_generations == 5
    assert FREE_POLICY.max_daily_generations is None
    assert TRAVEL_PLUS_POLICY.max_daily_generations == 30
    assert TRAVEL_PLUS_POLICY.max_weekly_generations is None


@pytest.mark.asyncio
async def test_require_generation_quota_raises_when_weekly_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tourism_backend.modules.route_builder.application import quota as quota_mod

    async def _fake_count(*_args: object, **_kwargs: object) -> int:
        return 5

    monkeypatch.setattr(quota_mod, "count_generations_since", _fake_count)

    class _Session:
        pass

    with pytest.raises(AppError) as exc:
        await quota_mod.require_generation_quota(
            _Session(),  # type: ignore[arg-type]
            user_id=uuid4(),
            policy=FREE_POLICY,
        )
    assert exc.value.code == "generation_quota_exceeded"
