"""Assign a default cover from existing route/place photos.

No default avatar any more (BACKEND-76): who has no picture of their own is
shown as their companion character's portrait in the app (FRONTEND-97); a
random place photo in its place read as someone else's picture.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.media.application import service as media_service


async def ensure_default_user_media(session: AsyncSession, user_id: UUID) -> None:
    covers = await media_service.list_reusable_covers(session, limit=40)
    if not covers:
        return

    cover_url = await media_service.resolve_url(
        session, entity_type="user", entity_id=user_id, role="cover"
    )
    cover_source = covers[user_id.int % len(covers)]

    if cover_url is None:
        await media_service.replace_attachment(
            session,
            entity_type="user",
            entity_id=user_id,
            role="cover",
            storage_key=cover_source.storage_key,
            content_type=cover_source.content_type,
            alt_text="Default profile cover",
        )
