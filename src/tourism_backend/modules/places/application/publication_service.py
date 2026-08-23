"""Load publication-readiness facts for places from the database.

Thin DB layer over `publication_readiness`, which stays pure. Kept in the
places module so the admin view and any CLI share one definition of "ready"
— a second, drifting copy of the rule is exactly how a review gate rots.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.places.application.publication_readiness import (
    PlacePublicationFacts,
)
from tourism_backend.modules.places.infrastructure.models import (
    Place,
    PlaceCategory,
    PlaceImage,
)


async def facts_for_places(
    session: AsyncSession,
    place_ids: list[UUID],
) -> dict[UUID, PlacePublicationFacts]:
    if not place_ids:
        return {}

    places = list((await session.scalars(select(Place).where(Place.id.in_(place_ids)))).all())

    category_rows = (
        await session.execute(
            select(PlaceCategory.place_id, func.count())
            .where(PlaceCategory.place_id.in_(place_ids))
            .group_by(PlaceCategory.place_id)
        )
    ).all()
    category_counts: dict[UUID, int] = {row[0]: int(row[1]) for row in category_rows}
    covered = set(
        (
            await session.scalars(
                select(PlaceImage.place_id).where(
                    PlaceImage.place_id.in_(place_ids),
                    PlaceImage.is_cover.is_(True),
                    PlaceImage.status == "active",
                )
            )
        ).all()
    )

    return {
        place.id: PlacePublicationFacts(
            name=place.name,
            has_locality=place.locality_id is not None,
            category_count=int(category_counts.get(place.id, 0)),
            short_description=place.short_description,
            description=place.description,
            content_enrichment_status=place.content_enrichment_status,
            has_cover_photo=place.id in covered,
            temporary_closure_status=place.temporary_closure_status,
        )
        for place in places
    }
