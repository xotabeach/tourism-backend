"""Idempotent grants in their own transaction, after the user's action commits."""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import get_settings
from tourism_backend.modules.achievements.facts import historical_unlock_time, progress
from tourism_backend.modules.achievements.queries import collect
from tourism_backend.modules.achievements.rules import RULES
from tourism_backend.modules.identity.infrastructure.models import Achievement, UserAchievement
from tourism_backend.modules.notifications.application.service import (
    create_achievement_notification,
    maybe_push_notification,
)

logger = logging.getLogger(__name__)


async def evaluate(
    session: AsyncSession,
    user_id: UUID,
    triggers: set[str] | None = None,
    *,
    backfill: bool = False,
) -> list[Achievement]:
    now = datetime.now(UTC)
    facts = await collect(session, user_id, historical=backfill)
    if facts.flagged:
        return []
    values = progress(facts, now)
    catalog = list(
        (await session.scalars(select(Achievement).order_by(Achievement.sort_order))).all()
    )
    granted = []
    for badge in catalog:
        rule = RULES.get(badge.slug)
        if rule is None or rule.soon or rule.slug in facts.soon:
            continue
        if triggers is not None and not rule.triggers.intersection(triggers):
            continue
        value = values.get("_marathoner_award" if rule.slug == "marathoner" else rule.slug, 0)
        if value < rule.target:
            continue
        unlocked_at = (
            historical_unlock_time(facts, badge.slug, rule.target, now) if backfill else now
        )
        new = await session.scalar(
            insert(UserAchievement)
            .values(
                user_id=user_id,
                achievement_id=badge.id,
                unlocked_at=unlocked_at,
                source="backfill" if backfill else "rule",
                celebrated_at=unlocked_at if backfill else None,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "achievement_id"])
            .returning(UserAchievement.achievement_id)
        )
        if new is None:
            continue
        granted.append(badge)
        if not backfill:
            await create_achievement_notification(
                session, user_id=user_id, achievement_id=badge.id, title=badge.title
            )
    return granted


async def push_grants(session: AsyncSession, user_id: UUID, badges: list[Achievement]) -> None:
    if not badges:
        return
    single = len(badges) == 1
    count = len(badges)
    noun = (
        "достижений"
        if 11 <= count % 100 <= 14
        else (
            "достижения"
            if 2 <= count % 10 <= 4
            else "достижение"
            if count % 10 == 1
            else "достижений"
        )
    )
    await maybe_push_notification(
        session,
        get_settings(),
        user_id=user_id,
        kind="achievement_unlocked",
        title="Вы получили достижение" if single else f"Вы получили {count} {noun}",
        body=badges[0].title if single else "Откройте список достижений",
        target_type="achievement",
        target_id=badges[0].id if single else None,
    )


async def after_commit(
    session: AsyncSession, user_id: UUID, triggers: set[str] | None = None
) -> None:
    """Failure must never invalidate the already committed business action."""
    try:
        async with AsyncSession(bind=session.bind, expire_on_commit=False) as awards:
            async with awards.begin():
                granted = await evaluate(awards, user_id, triggers)
            await push_grants(awards, user_id, granted)
    except Exception:
        logger.exception("achievement_evaluation_failed user_id=%s triggers=%s", user_id, triggers)
