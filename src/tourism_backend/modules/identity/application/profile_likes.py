"""Profile like / unlike (points granted lazily after 6h)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import get_settings
from tourism_backend.modules.identity.application.travel_points import grant_due_travel_points
from tourism_backend.modules.identity.infrastructure.models import ProfileLike, User
from tourism_backend.modules.notifications.application import service as notifications_service


async def like_profile(session: AsyncSession, *, actor_id: UUID, target_user_id: UUID) -> None:
    if actor_id == target_user_id:
        raise AppError(
            code="validation_error",
            message="Cannot like your own profile",
            status_code=400,
        )
    target = await session.get(User, target_user_id)
    if target is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)

    existing = await session.get(ProfileLike, (actor_id, target_user_id))
    if existing is not None:
        await grant_due_travel_points(session)
        return

    session.add(
        ProfileLike(
            liker_id=actor_id,
            liked_user_id=target_user_id,
            created_at=datetime.now(UTC),
            awarded_at=None,
        )
    )
    notif = await notifications_service.create_profile_like_notification(
        session,
        recipient_user_id=target_user_id,
        actor_user_id=actor_id,
    )
    await session.commit()
    if notif is not None:
        await notifications_service.maybe_push_notification(
            session,
            get_settings(),
            user_id=target_user_id,
            kind=notif.kind,
            title=notif.title,
            body=notif.body,
            target_type="user",
            target_id=actor_id,
        )
    await grant_due_travel_points(session)


async def unlike_profile(session: AsyncSession, *, actor_id: UUID, target_user_id: UUID) -> None:
    existing = await session.get(ProfileLike, (actor_id, target_user_id))
    if existing is None:
        return
    # Removing before award drops the pending grant; after award points stay.
    await session.delete(existing)
    await session.commit()
