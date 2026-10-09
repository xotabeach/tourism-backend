"""Photos and videos of an author's route."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.routes.application.media import SavedRouteMedia
from tourism_backend.modules.routes.application.route_drafts import (
    _owned_editable_route,
)
from tourism_backend.modules.routes.application.schemas import (
    UserRouteMediaOut,
)


async def clear_user_route_media(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> None:
    await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    await session.execute(
        update(MediaAttachment)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == route_id,
            MediaAttachment.status == "active",
        )
        .values(status="archived", updated_at=datetime.now(UTC))
    )
    await session.commit()


async def sync_user_route_media(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
    keep: Sequence[UUID],
) -> None:
    """Keeps exactly [keep], in that order, and archives the rest.

    The editor used to have only "archive everything, then upload again",
    which it could only do for files it still held on disk. A draft reopened
    from the server has none of them, so saving it wiped its photos (reported
    2026-09-08). With this the client keeps what is already stored, uploads
    only what is new, and ``keep``'s order is the gallery's order.
    """
    await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    now = datetime.now(UTC)
    attachments = list(
        (
            await session.scalars(
                select(MediaAttachment).where(
                    MediaAttachment.entity_type == "route",
                    MediaAttachment.entity_id == route_id,
                    MediaAttachment.status == "active",
                )
            )
        ).all()
    )
    positions = {media_id: index for index, media_id in enumerate(keep)}
    for attachment in attachments:
        position = positions.get(attachment.id)
        if position is None:
            attachment.status = "archived"
        else:
            attachment.sort_order = position
        attachment.updated_at = now
    await session.flush()
    await _restamp_route_media_cover(session, route_id=route_id, now=now)
    await session.commit()


async def _restamp_route_media_cover(
    session: AsyncSession,
    *,
    route_id: UUID,
    now: datetime,
) -> None:
    """Makes the first remaining image the cover.

    The cover is picked once, when a file is uploaded. Once media can be
    removed and reordered without re-uploading, that stamp goes stale — the
    cover could be archived, or sit in the middle of the gallery.
    """
    attachments = list(
        (
            await session.scalars(
                select(MediaAttachment)
                .where(
                    MediaAttachment.entity_type == "route",
                    MediaAttachment.entity_id == route_id,
                    MediaAttachment.status == "active",
                )
                .order_by(MediaAttachment.sort_order, MediaAttachment.created_at)
            )
        ).all()
    )
    cover = next(
        (item for item in attachments if (item.content_type or "").startswith("image/")),
        None,
    )
    # Demote first and flush: `uq_media_attachments_one_cover` allows a
    # single active cover per entity, so promoting the new one in the same
    # flush as the old one's demotion trips the index.
    for attachment in attachments:
        if attachment.role == "cover" and attachment is not cover:
            attachment.role = "gallery"
            attachment.updated_at = now
    await session.flush()
    if cover is not None and cover.role != "cover":
        cover.role = "cover"
        cover.updated_at = now
        await session.flush()


async def ensure_user_route_editable(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
) -> None:
    await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )


async def add_user_route_media(
    session: AsyncSession,
    *,
    route_id: UUID,
    owner_user_id: UUID,
    position: int,
    saved: SavedRouteMedia,
) -> UserRouteMediaOut:
    await _owned_editable_route(
        session,
        route_id=route_id,
        owner_user_id=owner_user_id,
    )
    active_count = int(
        await session.scalar(
            select(func.count()).where(
                MediaAttachment.entity_type == "route",
                MediaAttachment.entity_id == route_id,
                MediaAttachment.status == "active",
            )
        )
        or 0
    )
    if active_count >= 10:
        raise AppError(
            code="route_media_limit",
            message="A route can contain at most 10 media files",
            status_code=400,
        )

    has_cover = bool(
        await session.scalar(
            select(func.count()).where(
                MediaAttachment.entity_type == "route",
                MediaAttachment.entity_id == route_id,
                MediaAttachment.role == "cover",
                MediaAttachment.status == "active",
            )
        )
    )
    role = "cover" if saved.kind == "image" and not has_cover else "gallery"
    attachment = MediaAttachment(
        id=uuid4(),
        entity_type="route",
        entity_id=route_id,
        role=role,
        storage_key=saved.storage_key,
        public_path=saved.public_path,
        content_type=saved.content_type,
        byte_size=saved.byte_size,
        width=saved.width,
        height=saved.height,
        checksum_sha256=saved.checksum_sha256,
        status="active",
        uploaded_by_user_id=owner_user_id,
        sort_order=position,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(attachment)
    await session.commit()
    return UserRouteMediaOut(
        id=attachment.id,
        public_path=attachment.public_path,
        kind=saved.kind,
        position=position,
    )
