"""Assign bundled-style default avatar/cover from existing route/place photos."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.media.application import service as media_service


async def ensure_default_user_media(session: AsyncSession, user_id: UUID) -> None:
    covers = await media_service.list_reusable_covers(session, limit=40)
    if not covers:
        return

    avatar_url = await media_service.resolve_url(
        session, entity_type="user", entity_id=user_id, role="avatar"
    )
    cover_url = await media_service.resolve_url(
        session, entity_type="user", entity_id=user_id, role="cover"
    )
    cover_source = covers[user_id.int % len(covers)]
    avatar_source = covers[(user_id.int // 7) % len(covers)]

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
    if avatar_url is None:
        await media_service.replace_attachment(
            session,
            entity_type="user",
            entity_id=user_id,
            role="avatar",
            storage_key=avatar_source.storage_key,
            content_type=avatar_source.content_type,
            alt_text="Default profile avatar",
        )
