"""Achievement catalog reads and starter grants (no live unlock rules yet)."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.application.achievement_schemas import (
    AchievementListOut,
    AchievementOut,
)
from tourism_backend.modules.identity.infrastructure.models import (
    Achievement,
    User,
    UserAchievement,
)
from tourism_backend.modules.notifications.application import service as notifications_service
from tourism_backend.modules.notifications.infrastructure.models import Notification

_STARTER_MIN = 5
_STARTER_MAX = 15


def _out(row: Achievement, *, unlocked_at: datetime | None) -> AchievementOut:
    return AchievementOut(
        id=str(row.id),
        slug=row.slug,
        title=row.title,
        description=row.description,
        how_to_earn=row.how_to_earn or row.description,
        icon_slug=row.icon_slug or row.slug,
        is_unlocked=unlocked_at is not None,
        unlocked_at=unlocked_at,
    )


async def list_for_user(session: AsyncSession, user_id: UUID) -> AchievementListOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)

    catalog = list(
        (
            await session.scalars(
                select(Achievement).order_by(Achievement.sort_order, Achievement.id)
            )
        ).all()
    )
    unlocked_rows = list(
        (
            await session.scalars(select(UserAchievement).where(UserAchievement.user_id == user_id))
        ).all()
    )
    unlocked_at = {row.achievement_id: row.unlocked_at for row in unlocked_rows}
    items = [_out(row, unlocked_at=unlocked_at.get(row.id)) for row in catalog]
    unlocked_count = sum(1 for item in items if item.is_unlocked)
    return AchievementListOut(
        items=items,
        unlocked_count=unlocked_count,
        total=len(items),
    )


async def grant_random_starter_achievements(
    session: AsyncSession,
    *,
    user_id: UUID,
    notify: bool = False,
) -> Notification | None:
    """Assign 5–15 catalog badges. Optional single inbox+push notification."""
    already = await session.scalar(
        select(UserAchievement.achievement_id).where(UserAchievement.user_id == user_id).limit(1)
    )
    if already is not None:
        return None

    catalog = list((await session.scalars(select(Achievement))).all())
    if not catalog:
        return None

    rng = random.Random(user_id.int)  # noqa: S311 — deterministic starter set, not crypto
    count = min(len(catalog), rng.randint(_STARTER_MIN, _STARTER_MAX))
    picked = rng.sample(catalog, count)
    now = datetime.now(UTC)
    for row in picked:
        session.add(
            UserAchievement(
                user_id=user_id,
                achievement_id=row.id,
                unlocked_at=now,
            )
        )
    if not notify or not picked:
        return None
    badge = picked[0]
    return await notifications_service.create_achievement_notification(
        session,
        user_id=user_id,
        achievement_id=badge.id,
        title=badge.title,
    )
