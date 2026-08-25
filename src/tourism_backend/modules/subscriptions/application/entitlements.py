"""Quota / entitlement policy for free vs Travel+ plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from tourism_backend.api.errors import AppError
from tourism_backend.config import AppEnvironment


class HasTravelPlusFlag(Protocol):
    travel_plus_active: bool


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """Immutable plan limits. Magic numbers live only here."""

    plan_id: str
    ai_chat_enabled: bool
    max_weekly_generations: int | None
    max_daily_generations: int | None
    max_route_points: int
    alternatives_count: int
    advanced_filters_enabled: bool
    ads_enabled: bool
    exclusive_routes_enabled: bool
    travel_points_multiplier: float
    offline_favorites_extended: bool


FREE_POLICY = QuotaPolicy(
    plan_id="free",
    ai_chat_enabled=False,
    max_weekly_generations=5,
    max_daily_generations=None,
    max_route_points=5,
    alternatives_count=1,
    advanced_filters_enabled=False,
    ads_enabled=True,
    exclusive_routes_enabled=False,
    travel_points_multiplier=1.0,
    offline_favorites_extended=False,
)

TRAVEL_PLUS_POLICY = QuotaPolicy(
    plan_id="travel_plus",
    ai_chat_enabled=True,
    max_weekly_generations=None,
    max_daily_generations=30,
    max_route_points=12,
    alternatives_count=3,
    advanced_filters_enabled=True,
    ads_enabled=False,
    exclusive_routes_enabled=True,
    travel_points_multiplier=1.5,
    offline_favorites_extended=True,
)


def mock_self_activate_allowed(app_env: AppEnvironment | str | None) -> bool:
    """Self-serve mock checkout is a local/test convenience, never staging/prod.

    Real billing is not as-built; the HTTP activate path must not grant Travel+
    on internet-facing environments. Admin grants still use source='admin'.
    """
    if app_env is None:
        return False
    value = app_env.value if isinstance(app_env, AppEnvironment) else app_env
    return value in {AppEnvironment.LOCAL.value, AppEnvironment.TEST.value}


def policy_for_user(user: HasTravelPlusFlag) -> QuotaPolicy:
    """Resolve plan from denormalized Travel+ flag (SoT refreshed on /me)."""
    if user.travel_plus_active:
        return TRAVEL_PLUS_POLICY
    return FREE_POLICY


def require_ai_chat(user: HasTravelPlusFlag) -> QuotaPolicy:
    """Return Travel+ policy or raise for AI endpoints."""
    policy = policy_for_user(user)
    if not policy.ai_chat_enabled:
        raise AppError(
            code="travel_plus_required",
            message="Подбор с ИИ доступен только с активной подпиской Тревел+",
            status_code=403,
        )
    return policy
