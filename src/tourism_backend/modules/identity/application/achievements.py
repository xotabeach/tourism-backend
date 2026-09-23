"""Private progress and a separate, minimal public achievement feed."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.achievements.facts import progress
from tourism_backend.modules.achievements.queries import collect
from tourism_backend.modules.achievements.rules import RULES
from tourism_backend.modules.identity.application.achievement_schemas import (
    AchievementListOut,
    AchievementOut,
    AchievementProgress,
    PublicAchievementListOut,
    PublicAchievementOut,
)
from tourism_backend.modules.identity.infrastructure.models import (
    Achievement,
    User,
    UserAchievement,
)


async def list_for_user(session: AsyncSession, user_id: UUID) -> PublicAchievementListOut:
    if await session.get(User, user_id) is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    rows = (
        await session.scalars(
            select(Achievement)
            .join(UserAchievement, UserAchievement.achievement_id == Achievement.id)
            .where(UserAchievement.user_id == user_id)
            .order_by(Achievement.sort_order, Achievement.id)
        )
    ).all()
    items = [
        PublicAchievementOut(
            id=str(row.id),
            slug=row.slug,
            title=row.title,
            description=row.description,
            how_to_earn=row.how_to_earn or row.description,
            icon_slug=row.icon_slug or row.slug,
        )
        for row in rows
    ]
    return PublicAchievementListOut(items=items, unlocked_count=len(items), total=len(items))


async def list_for_owner(
    session: AsyncSession, user_id: UUID, *, uncelebrated: bool = False
) -> AchievementListOut:
    facts = await collect(session, user_id)
    values = progress(facts, datetime.now(UTC))
    awards = {
        row.achievement_id: row
        for row in (
            await session.scalars(select(UserAchievement).where(UserAchievement.user_id == user_id))
        ).all()
    }
    rows = (
        await session.scalars(select(Achievement).order_by(Achievement.sort_order, Achievement.id))
    ).all()
    items = []
    total = unlocked = 0
    for row in rows:
        rule = RULES.get(row.slug)
        soon = rule is None or rule.soon or row.slug in facts.soon
        award = awards.get(row.id)
        if not soon:
            total += 1
            unlocked += int(award is not None)
        if uncelebrated and (award is None or award.celebrated_at is not None):
            continue
        items.append(
            AchievementOut(
                id=str(row.id),
                slug=row.slug,
                title=row.title,
                description=row.description,
                how_to_earn=row.how_to_earn or row.description,
                icon_slug=row.icon_slug or row.slug,
                is_unlocked=award is not None,
                status="unlocked" if award else ("soon" if soon else "locked"),
                unlocked_at=award.unlocked_at if award else None,
                celebrated=award is not None and award.celebrated_at is not None,
                available=not soon,
                progress=AchievementProgress(current=values.get(row.slug, 0), target=rule.target)
                if rule and rule.counted and not soon
                else None,
            )
        )
    return AchievementListOut(items=items, unlocked_count=unlocked, total=total)


async def mark_celebrated(session: AsyncSession, user_id: UUID, ids: list[UUID]) -> None:
    await session.execute(
        update(UserAchievement)
        .where(
            UserAchievement.user_id == user_id,
            UserAchievement.achievement_id.in_(ids),
            UserAchievement.celebrated_at.is_(None),
        )
        .values(celebrated_at=datetime.now(UTC))
    )
    await session.commit()
