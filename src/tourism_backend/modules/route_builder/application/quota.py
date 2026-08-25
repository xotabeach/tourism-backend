"""Usage counters for route generation quotas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.route_builder.application.schemas import QuotaSnapshotOut
from tourism_backend.modules.route_builder.infrastructure.models import RouteGenerationEvent
from tourism_backend.modules.subscriptions.application.entitlements import QuotaPolicy


def _start_of_utc_day(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


def _start_of_utc_week(now: datetime) -> datetime:
    day = _start_of_utc_day(now)
    return day - timedelta(days=day.weekday())


async def count_generations_since(
    session: AsyncSession,
    *,
    user_id: UUID,
    since: datetime,
) -> int:
    value = await session.scalar(
        select(func.count())
        .select_from(RouteGenerationEvent)
        .where(
            RouteGenerationEvent.user_id == user_id,
            RouteGenerationEvent.created_at >= since,
        )
    )
    return int(value or 0)


def user_quota_lock_stmt(user_id: UUID) -> Select[tuple[UUID]]:
    """Serialize per-user generation so check-then-insert cannot overshoot (AI-5)."""
    return select(User.id).where(User.id == user_id).with_for_update()


async def require_generation_quota(
    session: AsyncSession,
    *,
    user_id: UUID,
    policy: QuotaPolicy,
    now: datetime | None = None,
) -> None:
    locked = await session.scalar(user_quota_lock_stmt(user_id))
    if locked is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    moment = now or datetime.now(UTC)
    if policy.max_daily_generations is not None:
        used = await count_generations_since(
            session,
            user_id=user_id,
            since=_start_of_utc_day(moment),
        )
        if used >= policy.max_daily_generations:
            raise AppError(
                code="generation_quota_exceeded",
                message="Достигнут дневной лимит генераций маршрута",
                status_code=429,
            )
    if policy.max_weekly_generations is not None:
        used = await count_generations_since(
            session,
            user_id=user_id,
            since=_start_of_utc_week(moment),
        )
        if used >= policy.max_weekly_generations:
            raise AppError(
                code="generation_quota_exceeded",
                message="Достигнут недельный лимит генераций маршрута",
                status_code=429,
            )


async def quota_snapshot(
    session: AsyncSession,
    *,
    user_id: UUID,
    policy: QuotaPolicy,
    now: datetime | None = None,
) -> QuotaSnapshotOut:
    moment = now or datetime.now(UTC)
    daily_used = await count_generations_since(
        session,
        user_id=user_id,
        since=_start_of_utc_day(moment),
    )
    weekly_used = await count_generations_since(
        session,
        user_id=user_id,
        since=_start_of_utc_week(moment),
    )
    daily_remaining = (
        None
        if policy.max_daily_generations is None
        else max(0, policy.max_daily_generations - daily_used)
    )
    weekly_remaining = (
        None
        if policy.max_weekly_generations is None
        else max(0, policy.max_weekly_generations - weekly_used)
    )
    return QuotaSnapshotOut(
        daily_used=daily_used,
        weekly_used=weekly_used,
        daily_remaining=daily_remaining,
        weekly_remaining=weekly_remaining,
    )
