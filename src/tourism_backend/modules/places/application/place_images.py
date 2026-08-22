"""Sync helper to upsert a `place_images` row (mirrors `seed_crimea.py`)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from tourism_backend.modules.places.infrastructure.models import PlaceImage


def upsert_place_image(
    session: Session,
    *,
    place_id: UUID,
    media_asset_id: UUID,
    source_url: str,
    is_cover: bool,
    author: str | None = None,
    license: str | None = None,  # noqa: A002 — matches PlaceImage.license column name
    alt_text: str | None = None,
    sort_order: int = 0,
) -> PlaceImage:
    now = datetime.now(UTC)
    image = session.scalar(
        select(PlaceImage).where(
            PlaceImage.place_id == place_id,
            PlaceImage.source_url == source_url,
        )
    )
    if image is None:
        image = PlaceImage(id=uuid4(), place_id=place_id, created_at=now, updated_at=now)
        session.add(image)

    if is_cover:
        for other in session.scalars(
            select(PlaceImage).where(
                PlaceImage.place_id == place_id,
                PlaceImage.is_cover.is_(True),
                PlaceImage.id != image.id,
            )
        ).all():
            other.is_cover = False
            other.updated_at = now

    image.kind = "photo"
    image.alt_text = alt_text
    image.author = author
    image.license = license
    image.source_url = source_url
    image.media_asset_id = media_asset_id
    image.sort_order = sort_order
    image.is_cover = is_cover
    image.status = "active"
    image.updated_at = now
    session.flush()
    return image
