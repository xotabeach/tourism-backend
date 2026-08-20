"""Unit tests for free vs Travel+ quota policy."""

from __future__ import annotations

import pytest

from tourism_backend.api.errors import AppError
from tourism_backend.modules.subscriptions.application.entitlements import (
    FREE_POLICY,
    TRAVEL_PLUS_POLICY,
    policy_for_user,
    require_ai_chat,
)


class _UserFlag:
    def __init__(self, *, travel_plus_active: bool) -> None:
        self.travel_plus_active = travel_plus_active


def test_free_policy_matches_product_contract() -> None:
    assert FREE_POLICY.ai_chat_enabled is False
    assert FREE_POLICY.max_weekly_generations == 5
    assert FREE_POLICY.max_route_points == 5
    assert FREE_POLICY.alternatives_count == 1
    assert FREE_POLICY.advanced_filters_enabled is False


def test_travel_plus_policy_matches_product_contract() -> None:
    assert TRAVEL_PLUS_POLICY.ai_chat_enabled is True
    assert TRAVEL_PLUS_POLICY.max_weekly_generations is None
    assert TRAVEL_PLUS_POLICY.max_daily_generations == 30
    assert TRAVEL_PLUS_POLICY.max_route_points == 12
    assert TRAVEL_PLUS_POLICY.alternatives_count == 3
    assert TRAVEL_PLUS_POLICY.advanced_filters_enabled is True


def test_policy_for_user_switches_on_flag() -> None:
    free = policy_for_user(_UserFlag(travel_plus_active=False))
    plus = policy_for_user(_UserFlag(travel_plus_active=True))
    assert free.plan_id == "free"
    assert plus.plan_id == "travel_plus"


def test_require_ai_chat_blocks_free() -> None:
    with pytest.raises(AppError) as exc:
        require_ai_chat(_UserFlag(travel_plus_active=False))
    assert exc.value.code == "travel_plus_required"
    assert exc.value.status_code == 403
