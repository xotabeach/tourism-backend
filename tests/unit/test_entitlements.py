"""Unit tests for free vs Travel+ quota policy."""

from __future__ import annotations

import pytest

from tourism_backend.config import AppEnvironment
from tourism_backend.modules.subscriptions.application.entitlements import (
    FREE_POLICY,
    TRAVEL_PLUS_POLICY,
    mock_self_activate_allowed,
    policy_for_user,
    require_ai_chat,
)


class _UserFlag:
    def __init__(self, *, travel_plus_active: bool) -> None:
        self.travel_plus_active = travel_plus_active


def test_free_policy_matches_product_contract() -> None:
    assert FREE_POLICY.ai_chat_enabled is True
    assert FREE_POLICY.max_weekly_generations is None
    assert FREE_POLICY.max_daily_generations == 5
    assert FREE_POLICY.max_daily_ai_replies == 30
    assert FREE_POLICY.max_route_points == 12
    assert FREE_POLICY.alternatives_count == 3
    assert FREE_POLICY.advanced_filters_enabled is True


def test_travel_plus_policy_matches_product_contract() -> None:
    assert TRAVEL_PLUS_POLICY.ai_chat_enabled is True
    assert TRAVEL_PLUS_POLICY.max_weekly_generations is None
    assert TRAVEL_PLUS_POLICY.max_daily_generations == 5
    assert TRAVEL_PLUS_POLICY.max_daily_ai_replies == 30
    assert TRAVEL_PLUS_POLICY.max_route_points == 12
    assert TRAVEL_PLUS_POLICY.alternatives_count == 3
    assert TRAVEL_PLUS_POLICY.advanced_filters_enabled is True


def test_policy_for_user_switches_on_flag() -> None:
    free = policy_for_user(_UserFlag(travel_plus_active=False))
    plus = policy_for_user(_UserFlag(travel_plus_active=True))
    assert free.plan_id == "free"
    assert plus.plan_id == "travel_plus"


def test_require_ai_chat_allows_free_during_beta() -> None:
    assert require_ai_chat(_UserFlag(travel_plus_active=False)) is FREE_POLICY


@pytest.mark.parametrize(
    "env",
    [
        AppEnvironment.LOCAL,
        AppEnvironment.TEST,
        AppEnvironment.STAGING,
        AppEnvironment.PRODUCTION,
        None,
        "production",
        "test",
    ],
)
def test_mock_self_activate_is_disabled_in_every_environment(
    env: AppEnvironment | str | None,
) -> None:
    assert mock_self_activate_allowed(env) is False
