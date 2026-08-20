from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import Select, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.geography.infrastructure.models import Locality, Region
from tourism_backend.modules.places.application.schemas import (
    CategoryOut,
    PlaceDetailOut,
    PlaceEntranceOut,
    PlaceListItemOut,
    PlaceListOut,
)
from tourism_backend.modules.places.infrastructure.models import (
    Category,
    Place,
    PlaceCategory,
    PlaceEntrance,
    PlaceImage,
)


async def list_categories(session: AsyncSession) -> list[CategoryOut]:
    rows = await session.scalars(
        select(Category)
        .where(Category.status == "active")
        .order_by(Category.sort_order, Category.name)
    )
    return [CategoryOut.model_validate(row) for row in rows.all()]


async def _cover_urls_for_places(
    session: AsyncSession,
    place_ids: list[UUID],
) -> dict[UUID, str]:
    if not place_ids:
        return {}
    from tourism_backend.modules.media.infrastructure.models import MediaAttachment

    attachment_url = func.coalesce(MediaAttachment.public_path, PlaceImage.source_url)
    stmt = (
        select(PlaceImage.place_id, attachment_url)
        .outerjoin(
            MediaAttachment,
            (MediaAttachment.id == PlaceImage.media_asset_id)
            & (MediaAttachment.status == "active"),
        )
        .where(
            PlaceImage.place_id.in_(place_ids),
            PlaceImage.status == "active",
            PlaceImage.is_cover.is_(True),
            attachment_url.is_not(None),
        )
    )
    return {
        place_id: source_url
        for place_id, source_url in (await session.execute(stmt)).all()
        if source_url
    }


async def _image_urls_for_place(session: AsyncSession, place_id: UUID) -> list[str]:
    """Return the active place gallery with the cover image first."""
    from tourism_backend.modules.media.infrastructure.models import MediaAttachment

    attachment_url = func.coalesce(MediaAttachment.public_path, PlaceImage.source_url)
    stmt = (
        select(attachment_url)
        .select_from(PlaceImage)
        .outerjoin(
            MediaAttachment,
            (MediaAttachment.id == PlaceImage.media_asset_id)
            & (MediaAttachment.status == "active"),
        )
        .where(
            PlaceImage.place_id == place_id,
            PlaceImage.status == "active",
            PlaceImage.kind == "photo",
            attachment_url.is_not(None),
        )
        .order_by(PlaceImage.is_cover.desc(), PlaceImage.sort_order, PlaceImage.id)
    )
    return [url for url in (await session.scalars(stmt)).all() if url]


async def _categories_for_places(
    session: AsyncSession,
    place_ids: list[UUID],
) -> dict[UUID, list[CategoryOut]]:
    if not place_ids:
        return {}
    stmt = (
        select(PlaceCategory.place_id, Category)
        .join(Category, Category.id == PlaceCategory.category_id)
        .where(PlaceCategory.place_id.in_(place_ids), Category.status == "active")
        .order_by(Category.sort_order, Category.name)
    )
    mapping: dict[UUID, list[CategoryOut]] = {place_id: [] for place_id in place_ids}
    for place_id, category in (await session.execute(stmt)).all():
        mapping[place_id].append(CategoryOut.model_validate(category))
    return mapping


async def _coords_for_place(session: AsyncSession, place_id: UUID) -> tuple[float, float]:
    geom = cast(Place.location, Geometry)
    row = (await session.execute(select(ST_X(geom), ST_Y(geom)).where(Place.id == place_id))).one()
    return float(row[0]), float(row[1])


async def _coords_for_places(
    session: AsyncSession,
    place_ids: list[UUID],
) -> dict[UUID, tuple[float, float]]:
    if not place_ids:
        return {}
    geom = cast(Place.location, Geometry)
    rows = (
        await session.execute(
            select(Place.id, ST_X(geom), ST_Y(geom)).where(Place.id.in_(place_ids))
        )
    ).all()
    return {
        place_id: (float(lng), float(lat))
        for place_id, lng, lat in rows
        if lng is not None and lat is not None
    }


async def list_places(
    session: AsyncSession,
    *,
    region_slug: str | None,
    locality_slug: str | None,
    category: str | None,
    q: str | None,
    difficulty: str | None,
    payment_status: str | None,
    is_suitable_for_children: bool | None,
    is_suitable_for_pets: bool | None,
    season: str | None,
    temporary_closure_status: str | None,
    limit: int,
    offset: int,
) -> PlaceListOut:
    stmt: Select[tuple[Place]] = select(Place).where(Place.publication_status == "published")
    if region_slug:
        stmt = stmt.join(Region, Region.id == Place.region_id).where(Region.slug == region_slug)
    if locality_slug:
        stmt = stmt.join(Locality, Locality.id == Place.locality_id).where(
            Locality.slug == locality_slug
        )
    if category:
        stmt = (
            stmt.join(PlaceCategory, PlaceCategory.place_id == Place.id)
            .join(Category, Category.id == PlaceCategory.category_id)
            .where((Category.slug == category) | (Category.code == category))
        )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Place.name.ilike(pattern))
    if difficulty:
        stmt = stmt.where(Place.difficulty == difficulty)
    if payment_status:
        stmt = stmt.where(Place.payment_status == payment_status)
    if is_suitable_for_children is not None:
        stmt = stmt.where(Place.is_suitable_for_children.is_(is_suitable_for_children))
    if is_suitable_for_pets is not None:
        stmt = stmt.where(Place.is_suitable_for_pets.is_(is_suitable_for_pets))
    if season:
        stmt = stmt.where(Place.seasonality.contains([season]))
    if temporary_closure_status:
        stmt = stmt.where(Place.temporary_closure_status == temporary_closure_status)

    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one())

    places = (
        await session.scalars(
            stmt.order_by(Place.name, Place.id).distinct().limit(limit).offset(offset)
        )
    ).all()
    place_ids = [place.id for place in places]
    coords = await _coords_for_places(session, place_ids)
    categories = await _categories_for_places(session, place_ids)
    covers = await _cover_urls_for_places(session, place_ids)

    items: list[PlaceListItemOut] = []
    for place in places:
        lng, lat = coords[place.id]
        items.append(
            PlaceListItemOut(
                id=place.id,
                region_id=place.region_id,
                locality_id=place.locality_id,
                name=place.name,
                slug=place.slug,
                short_description=place.short_description,
                lng=lng,
                lat=lat,
                difficulty=place.difficulty,
                is_paid=place.is_paid,
                payment_status=place.payment_status,
                is_suitable_for_children=place.is_suitable_for_children,
                is_suitable_for_pets=place.is_suitable_for_pets,
                recommended_visit_minutes=place.recommended_visit_minutes,
                publication_status=place.publication_status,
                categories=categories.get(place.id, []),
                cover_image_url=covers.get(place.id),
            )
        )
    return PlaceListOut(items=items, total=total, limit=limit, offset=offset)


async def get_place(session: AsyncSession, place_id: UUID) -> PlaceDetailOut:
    place = await session.get(Place, place_id)
    if place is None or place.publication_status != "published":
        raise AppError(code="place_not_found", message="Place not found", status_code=404)

    lng, lat = await _coords_for_place(session, place.id)
    categories = (await _categories_for_places(session, [place.id])).get(place.id, [])
    covers = await _cover_urls_for_places(session, [place.id])
    image_urls = await _image_urls_for_place(session, place.id)

    entrance_row = await session.scalar(
        select(PlaceEntrance).where(
            PlaceEntrance.place_id == place.id,
            PlaceEntrance.is_primary.is_(True),
            PlaceEntrance.status == "active",
        )
    )
    primary_entrance: PlaceEntranceOut | None = None
    if entrance_row is not None:
        e_coords = (
            await session.execute(
                select(
                    ST_X(cast(PlaceEntrance.location, Geometry)),
                    ST_Y(cast(PlaceEntrance.location, Geometry)),
                ).where(PlaceEntrance.id == entrance_row.id)
            )
        ).one()
        primary_entrance = PlaceEntranceOut(
            id=entrance_row.id,
            name=entrance_row.name,
            entrance_type=entrance_row.entrance_type,
            is_primary=entrance_row.is_primary,
            lng=float(e_coords[0]),
            lat=float(e_coords[1]),
            address_hint=entrance_row.address_hint,
        )

    return PlaceDetailOut(
        id=place.id,
        region_id=place.region_id,
        locality_id=place.locality_id,
        name=place.name,
        slug=place.slug,
        short_description=place.short_description,
        lng=lng,
        lat=lat,
        difficulty=place.difficulty,
        is_paid=place.is_paid,
        payment_status=place.payment_status,
        is_suitable_for_children=place.is_suitable_for_children,
        is_suitable_for_pets=place.is_suitable_for_pets,
        recommended_visit_minutes=place.recommended_visit_minutes,
        publication_status=place.publication_status,
        categories=categories,
        cover_image_url=covers.get(place.id),
        image_urls=image_urls,
        description=place.description,
        address=place.address,
        contact_phone=place.contact_phone,
        website_url=place.website_url,
        accessibility=place.accessibility,
        recommended_equipment=place.recommended_equipment,
        seasonality=place.seasonality,
        price_notes=place.price_notes,
        safety_warnings=place.safety_warnings,
        temporary_closure_status=place.temporary_closure_status,
        temporary_closure_reason=place.temporary_closure_reason,
        freshness_status=place.freshness_status,
        source_name=place.source_name,
        source_license=place.source_license,
        data_quality_status=place.data_quality_status,
        primary_entrance=primary_entrance,
    )
