import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.notifications.application import device_tokens, fcm
from tourism_backend.modules.notifications.application.schemas import (
    NotificationListOut,
    NotificationOut,
)
from tourism_backend.modules.notifications.infrastructure.models import Notification

logger = logging.getLogger(__name__)

_BODY_MAX = 500
_TITLE_MAX = 120


def _clip(value: str, limit: int) -> str:
    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


def _build_notification(
    *,
    user_id: UUID,
    kind: str,
    title: str,
    body: str,
    target_type: str | None,
    target_id: UUID | None,
    actor_user_id: UUID | None = None,
) -> Notification:
    return Notification(
        id=uuid4(),
        user_id=user_id,
        actor_user_id=actor_user_id,
        kind=kind,
        title=_clip(title, _TITLE_MAX),
        body=_clip(body, _BODY_MAX),
        target_type=target_type,
        target_id=target_id,
        is_read=False,
        created_at=datetime.now(UTC),
    )


async def create_route_review_notification(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    actor_user_id: UUID,
    route_id: UUID,
    route_name: str,
) -> Notification | None:
    """Create inbox row for route owner when a review is published."""
    if owner_user_id == actor_user_id:
        return None

    short_route = _clip(route_name, 48)
    notification = _build_notification(
        user_id=owner_user_id,
        actor_user_id=actor_user_id,
        kind="route_review",
        title="Новый отзыв",
        body=f"Оставил свой комментарий под вашим маршрутом «{short_route}»",
        target_type="route",
        target_id=route_id,
    )
    session.add(notification)
    return notification


async def create_route_moderation_notification(
    session: AsyncSession,
    *,
    owner_user_id: UUID,
    route_id: UUID,
    route_name: str,
    approved: bool,
) -> Notification:
    """Notify route author after ops approve/reject publication."""
    short_route = _clip(route_name, 48)
    if approved:
        kind = "route_published"
        title = "Маршрут опубликован"
        body = f"Ваш маршрут «{short_route}» прошёл модерацию и доступен путешественникам"
    else:
        kind = "route_rejected"
        title = "Маршрут на доработке"
        body = (
            f"Маршрут «{short_route}» вернули на доработку. Исправьте замечания и отправьте снова"
        )
    notification = _build_notification(
        user_id=owner_user_id,
        kind=kind,
        title=title,
        body=body,
        target_type="route",
        target_id=route_id,
    )
    session.add(notification)
    return notification


async def create_review_moderation_notification(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    route_id: UUID,
    route_name: str,
    approved: bool,
) -> Notification:
    """Notify review author after ops approve/reject their comment."""
    short_route = _clip(route_name, 48)
    if approved:
        kind = "review_published"
        title = "Отзыв опубликован"
        body = f"Ваш отзыв к маршруту «{short_route}» прошёл модерацию"
    else:
        kind = "review_rejected"
        title = "Отзыв отклонён"
        body = (
            f"Ваш отзыв к маршруту «{short_route}» не прошёл модерацию. Вы можете отправить новый"
        )
    notification = _build_notification(
        user_id=author_user_id,
        kind=kind,
        title=title,
        body=body,
        target_type="route",
        target_id=route_id,
    )
    session.add(notification)
    return notification


async def create_profile_like_notification(
    session: AsyncSession,
    *,
    recipient_user_id: UUID,
    actor_user_id: UUID,
) -> Notification | None:
    """Notify profile owner when another user likes (subscribes to) their profile."""
    if recipient_user_id == actor_user_id:
        return None
    notification = _build_notification(
        user_id=recipient_user_id,
        actor_user_id=actor_user_id,
        kind="profile_like",
        title="Новая подписка",
        body="Подписался на ваш профиль",
        target_type="user",
        target_id=actor_user_id,
    )
    session.add(notification)
    return notification


async def create_achievement_notification(
    session: AsyncSession,
    *,
    user_id: UUID,
    achievement_id: UUID,
    title: str,
) -> Notification:
    """In-app (and later FCM) row when a traveler unlocks a badge."""
    short_title = _clip(title, 48)
    notification = _build_notification(
        user_id=user_id,
        kind="achievement_unlocked",
        title="Новое достижение",
        body=f"Получено достижение «{short_title}»",
        target_type="achievement",
        target_id=achievement_id,
    )
    session.add(notification)
    return notification


async def maybe_push_notification(
    session: AsyncSession,
    settings: Settings,
    *,
    user_id: UUID,
    kind: str,
    title: str,
    body: str,
    target_type: str,
    target_id: UUID,
) -> None:
    """Best-effort FCM when user opted into push and tokens exist."""
    user = await session.get(User, user_id)
    if user is None or not user.notify_push_enabled:
        logger.info("fcm_skipped_push_disabled user=%s", user_id)
        return
    tokens = await device_tokens.list_tokens_for_user(session, user_id=user_id)
    if not tokens:
        logger.info("fcm_skipped_no_tokens user=%s", user_id)
        return
    await fcm.send_data_message(
        settings,
        tokens=[row.token for row in tokens],
        title=title,
        body=body,
        data={
            "kind": kind,
            "target_type": target_type,
            "target_id": str(target_id),
        },
    )


async def maybe_push_route_review(
    session: AsyncSession,
    settings: Settings,
    *,
    owner_user_id: UUID,
    title: str,
    body: str,
    route_id: UUID,
) -> None:
    """Back-compat wrapper for route_review push."""
    await maybe_push_notification(
        session,
        settings,
        user_id=owner_user_id,
        kind="route_review",
        title=title,
        body=body,
        target_type="route",
        target_id=route_id,
    )


async def list_notifications(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int,
    offset: int,
) -> NotificationListOut:
    unread = int(
        await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        )
        or 0
    )
    rows = list(
        (
            await session.scalars(
                select(Notification)
                .where(Notification.user_id == user_id)
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    actor_ids = [row.actor_user_id for row in rows if row.actor_user_id is not None]
    names: dict[UUID, str] = {}
    if actor_ids:
        for user in (await session.scalars(select(User).where(User.id.in_(actor_ids)))).all():
            names[user.id] = user.display_name

    items = [
        NotificationOut(
            id=str(row.id),
            kind=row.kind,  # type: ignore[arg-type]
            title=row.title,
            body=row.body,
            actor_user_id=str(row.actor_user_id) if row.actor_user_id else None,
            actor_display_name=names.get(row.actor_user_id) if row.actor_user_id else None,
            target_type=row.target_type,  # type: ignore[arg-type]
            target_id=str(row.target_id) if row.target_id else None,
            is_read=row.is_read,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return NotificationListOut(items=items, unread_count=unread)


async def mark_notification_read(
    session: AsyncSession,
    *,
    user_id: UUID,
    notification_id: UUID,
) -> NotificationOut:
    row = await session.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    if row is None:
        raise AppError(
            code="notification_not_found",
            message="Notification not found",
            status_code=404,
        )
    row.is_read = True
    await session.commit()
    await session.refresh(row)
    actor_name = None
    if row.actor_user_id is not None:
        actor = await session.get(User, row.actor_user_id)
        actor_name = actor.display_name if actor is not None else None
    return NotificationOut(
        id=str(row.id),
        kind=row.kind,  # type: ignore[arg-type]
        title=row.title,
        body=row.body,
        actor_user_id=str(row.actor_user_id) if row.actor_user_id else None,
        actor_display_name=actor_name,
        target_type=row.target_type,  # type: ignore[arg-type]
        target_id=str(row.target_id) if row.target_id else None,
        is_read=row.is_read,
        created_at=row.created_at,
    )


async def mark_all_notifications_read(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> dict[str, int]:
    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    await session.commit()
    updated = getattr(result, "rowcount", 0) or 0
    return {"updated": int(updated)}
