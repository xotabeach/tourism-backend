"""Request-time photo resolution for places.

Only ~4/20 published places currently have a real photo (the Wikimedia
import pipeline in `photo_import.py` backfills the rest offline). Until that
backfill runs, AI-chat route/place cards would otherwise render with no
image at all. `generic_fallback_cover` gives every card *some* photo — any
one active *published* place photo already in the DB — rather than leaving
it blank.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.infrastructure.models import Place, PlaceImage


def published_place_attachment_covers_stmt(
    place_ids: list[UUID],
) -> Select[tuple[UUID, str]]:
    """Cover attachments only for places currently in the public catalog."""
    return select(MediaAttachment.entity_id, MediaAttachment.public_path).where(
        MediaAttachment.entity_type == "place",
        MediaAttachment.entity_id.in_(place_ids),
        MediaAttachment.role == "cover",
        MediaAttachment.status == "active",
        MediaAttachment.entity_id.in_(
            select(Place.id).where(Place.publication_status == "published")
        ),
    )


def published_place_image_covers_stmt(place_ids: list[UUID]) -> Select[tuple[UUID, str | None]]:
    return (
        select(PlaceImage.place_id, PlaceImage.source_url)
        .join(Place, Place.id == PlaceImage.place_id)
        .where(
            PlaceImage.place_id.in_(place_ids),
            PlaceImage.status == "active",
            PlaceImage.source_url.is_not(None),
            Place.publication_status == "published",
        )
        .order_by(PlaceImage.place_id, PlaceImage.is_cover.desc(), PlaceImage.sort_order)
    )


def generic_published_attachment_cover_stmt() -> Select[tuple[str]]:
    return (
        select(MediaAttachment.public_path)
        .where(
            MediaAttachment.entity_type == "place",
            MediaAttachment.role == "cover",
            MediaAttachment.status == "active",
            MediaAttachment.entity_id.in_(
                select(Place.id).where(Place.publication_status == "published")
            ),
        )
        .order_by(MediaAttachment.entity_id)
        .limit(1)
    )


def generic_published_place_image_cover_stmt() -> Select[tuple[str | None]]:
    return (
        select(PlaceImage.source_url)
        .join(Place, Place.id == PlaceImage.place_id)
        .where(
            PlaceImage.status == "active",
            PlaceImage.source_url.is_not(None),
            Place.publication_status == "published",
        )
        .order_by(PlaceImage.is_cover.desc(), PlaceImage.place_id)
        .limit(1)
    )


async def covers_for_places(session: AsyncSession, place_ids: list[UUID]) -> dict[UUID, str]:
    """Best available photo URL per *published* place: attachment cover first,
    else the active `PlaceImage` (import-pipeline photo)."""
    if not place_ids:
        return {}
    direct = await session.execute(published_place_attachment_covers_stmt(place_ids))
    covers = {place_id: path for place_id, path in direct.all() if path}
    remaining = [pid for pid in place_ids if pid not in covers]
    if not remaining:
        return covers
    fallback = await session.execute(published_place_image_covers_stmt(remaining))
    for place_id, source_url in fallback.all():
        if place_id not in covers and source_url:
            covers[place_id] = source_url
    return covers


async def generic_fallback_cover(session: AsyncSession) -> str | None:
    """Any one published place photo — last-resort filler, deterministic
    (ordered, not random) so it doesn't churn between requests."""
    direct = await session.scalar(generic_published_attachment_cover_stmt())
    if direct:
        return direct
    return await session.scalar(generic_published_place_image_cover_stmt())
