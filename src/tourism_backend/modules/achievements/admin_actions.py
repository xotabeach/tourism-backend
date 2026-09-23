"""Audited operator overrides. Revocation is explicit, never automatic."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.achievements.queries import collect
from tourism_backend.modules.achievements.rules import RULES
from tourism_backend.modules.achievements.service import push_grants
from tourism_backend.modules.identity.infrastructure.models import (
    Achievement,
    AchievementAdminAction,
    User,
    UserAchievement,
)
from tourism_backend.modules.notifications.application.service import (
    create_achievement_notification,
)
from tourism_backend.modules.notifications.infrastructure.models import Notification


async def apply(
    session: AsyncSession,
    *,
    user_id: UUID,
    achievement_id: UUID,
    admin_id: UUID,
    action: str,
    reason: str,
) -> None:
    reason = reason.strip()
    if action not in {"grant", "revoke"} or not reason or len(reason) > 2000:
        raise AppError(
            code="invalid_achievement_action",
            message="Укажите действие и причину до 2000 символов",
            status_code=422,
        )
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    badge = await session.get(Achievement, achievement_id)
    if user is None or badge is None:
        raise AppError(
            code="achievement_not_found",
            message="Пользователь или достижение не найдены",
            status_code=404,
        )
    now = datetime.now(UTC)
    granted = False
    if action == "grant":
        rule = RULES.get(badge.slug)
        facts = await collect(session, user_id)
        if rule is None or rule.soon or badge.slug in facts.soon:
            raise AppError(
                code="achievement_soon",
                message="Это достижение пока нельзя получить",
                status_code=409,
            )
        granted = (
            await session.scalar(
                insert(UserAchievement)
                .values(
                    user_id=user_id,
                    achievement_id=achievement_id,
                    unlocked_at=now,
                    source="operator",
                    reason=reason,
                    granted_by_admin_id=admin_id,
                )
                .on_conflict_do_nothing(index_elements=["user_id", "achievement_id"])
                .returning(UserAchievement.achievement_id)
            )
            is not None
        )
        if granted:
            await create_achievement_notification(
                session, user_id=user_id, achievement_id=achievement_id, title=badge.title
            )
    else:
        await session.execute(
            delete(UserAchievement).where(
                UserAchievement.user_id == user_id, UserAchievement.achievement_id == achievement_id
            )
        )
        await session.execute(
            delete(Notification).where(
                Notification.user_id == user_id,
                Notification.kind == "achievement_unlocked",
                Notification.target_id == achievement_id,
            )
        )
    session.add(
        AchievementAdminAction(
            user_id=user_id,
            achievement_id=achievement_id,
            admin_id=admin_id,
            action=action,
            reason=reason,
            created_at=now,
        )
    )
    await session.commit()
    if granted:
        await push_grants(session, user_id, [badge])
