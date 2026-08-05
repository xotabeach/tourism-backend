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

_BODY_MAX = 500
_TITLE_MAX = 120


def _clip(value: str, limit: int) -> str:
    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)] + "…"


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

    actor = await session.get(User, actor_user_id)
    actor_name = (actor.display_name if actor is not None else "Путешественник").strip()
    if not actor_name:
        actor_name = "Путешественник"

    short_route = _clip(route_name, 48)
    body = _clip(
        f"Оставил свой комментарий под вашим маршрутом «{short_route}»",
        _BODY_MAX,
    )
    notification = Notification(
        id=uuid4(),
        user_id=owner_user_id,
        actor_user_id=actor_user_id,
        kind="route_review",
        title=_clip("Новый отзыв", _TITLE_MAX),
        body=body,
        target_type="route",
        target_id=route_id,
        is_read=False,
        created_at=datetime.now(UTC),
    )
    session.add(notification)
    return notification


async def maybe_push_route_review(
    session: AsyncSession,
    settings: Settings,
    *,
    owner_user_id: UUID,
    title: str,
    body: str,
    route_id: UUID,
) -> None:
    """Best-effort FCM when owner opted into push and tokens exist."""
    owner = await session.get(User, owner_user_id)
    if owner is None or not owner.notify_push_enabled:
        return
    tokens = await device_tokens.list_tokens_for_user(session, user_id=owner_user_id)
    if not tokens:
        return
    await fcm.send_data_message(
        settings,
        tokens=[row.token for row in tokens],
        title=title,
        body=body,
        data={
            "kind": "route_review",
            "target_type": "route",
            "target_id": str(route_id),
        },
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
