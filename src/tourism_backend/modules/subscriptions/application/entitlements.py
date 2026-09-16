"""Quota / entitlement policy for free vs Travel+ plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class HasTravelPlusFlag(Protocol):
    travel_plus_active: bool


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """Immutable plan limits. Magic numbers live only here."""

    plan_id: str
    ai_chat_enabled: bool
    max_weekly_generations: int | None
    max_daily_generations: int | None
    max_daily_ai_replies: int
    max_route_points: int
    alternatives_count: int
    advanced_filters_enabled: bool
    ads_enabled: bool
    exclusive_routes_enabled: bool
    travel_points_multiplier: float
    offline_favorites_extended: bool


FREE_POLICY = QuotaPolicy(
    plan_id="free",
    ai_chat_enabled=True,
    max_weekly_generations=None,
    max_daily_generations=5,
    max_daily_ai_replies=30,
    max_route_points=12,
    alternatives_count=3,
    advanced_filters_enabled=True,
    ads_enabled=True,
    exclusive_routes_enabled=False,
    travel_points_multiplier=1.0,
    offline_favorites_extended=False,
)

TRAVEL_PLUS_POLICY = QuotaPolicy(
    plan_id="travel_plus",
    ai_chat_enabled=True,
    max_weekly_generations=None,
    max_daily_generations=5,
    max_daily_ai_replies=30,
    max_route_points=12,
    alternatives_count=3,
    advanced_filters_enabled=True,
    ads_enabled=False,
    exclusive_routes_enabled=True,
    travel_points_multiplier=1.5,
    offline_favorites_extended=True,
)


def mock_self_activate_allowed(app_env: object = None) -> bool:
    """Mock checkout is disabled in every environment during beta."""
    del app_env
    return False


def policy_for_user(user: HasTravelPlusFlag) -> QuotaPolicy:
    """Resolve plan from denormalized Travel+ flag (SoT refreshed on /me)."""
    if user.travel_plus_active:
        return TRAVEL_PLUS_POLICY
    return FREE_POLICY


def require_ai_chat(user: HasTravelPlusFlag) -> QuotaPolicy:
    """Return the user's policy; AI chat is open to everyone during beta."""
    return policy_for_user(user)
