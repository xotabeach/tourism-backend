from uuid import UUID

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import Select, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.geography.application.schemas import (
    CountryOut,
    LocalityOut,
    RegionOut,
)
from tourism_backend.modules.geography.infrastructure.models import Country, Locality, Region


async def list_countries(session: AsyncSession) -> list[CountryOut]:
    result = await session.scalars(
        select(Country).where(Country.status == "active").order_by(Country.name)
    )
    return [CountryOut.model_validate(row) for row in result.all()]


async def _coords_for_regions(
    session: AsyncSession,
    region_ids: list[UUID],
) -> dict[UUID, tuple[float | None, float | None]]:
    if not region_ids:
        return {}
    geom = cast(Region.center, Geometry)
    rows = (
        await session.execute(
            select(Region.id, ST_X(geom), ST_Y(geom)).where(Region.id.in_(region_ids))
        )
    ).all()
    return {
        region_id: (
            float(lng) if lng is not None else None,
            float(lat) if lat is not None else None,
        )
        for region_id, lng, lat in rows
    }


async def _coords_for_localities(
    session: AsyncSession,
    locality_ids: list[UUID],
) -> dict[UUID, tuple[float | None, float | None]]:
    if not locality_ids:
        return {}
    geom = cast(Locality.center, Geometry)
    rows = (
        await session.execute(
            select(Locality.id, ST_X(geom), ST_Y(geom)).where(Locality.id.in_(locality_ids))
        )
    ).all()
    return {
        locality_id: (
            float(lng) if lng is not None else None,
            float(lat) if lat is not None else None,
        )
        for locality_id, lng, lat in rows
    }


async def list_regions(session: AsyncSession, *, country_code: str | None) -> list[RegionOut]:
    stmt: Select[tuple[Region]] = select(Region).where(Region.status == "active")
    if country_code:
        stmt = stmt.join(Country, Country.id == Region.country_id).where(
            Country.code == country_code.upper()
        )
    stmt = stmt.order_by(Region.name)
    regions = (await session.scalars(stmt)).all()
    coords = await _coords_for_regions(session, [region.id for region in regions])

    out: list[RegionOut] = []
    for region in regions:
        lng, lat = coords.get(region.id, (None, None))
        payload = RegionOut.model_validate(region)
        out.append(payload.model_copy(update={"center_lng": lng, "center_lat": lat}))
    return out


async def list_localities(session: AsyncSession, *, region_slug: str) -> list[LocalityOut]:
    stmt = (
        select(Locality)
        .join(Region, Region.id == Locality.region_id)
        .where(Locality.status == "active", Region.slug == region_slug)
        .order_by(Locality.name)
    )
    localities = (await session.scalars(stmt)).all()
    coords = await _coords_for_localities(session, [locality.id for locality in localities])
    out: list[LocalityOut] = []
    for locality in localities:
        lng, lat = coords.get(locality.id, (None, None))
        payload = LocalityOut.model_validate(locality)
        out.append(payload.model_copy(update={"center_lng": lng, "center_lat": lat}))
    return out
