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


async def _coords_for_region(
    session: AsyncSession,
    region_id: object,
) -> tuple[float | None, float | None]:
    geom = cast(Region.center, Geometry)
    row = (
        await session.execute(select(ST_X(geom), ST_Y(geom)).where(Region.id == region_id))
    ).one_or_none()
    if row is None or row[0] is None:
        return None, None
    return float(row[0]), float(row[1])


async def _coords_for_locality(
    session: AsyncSession,
    locality_id: object,
) -> tuple[float | None, float | None]:
    geom = cast(Locality.center, Geometry)
    row = (
        await session.execute(select(ST_X(geom), ST_Y(geom)).where(Locality.id == locality_id))
    ).one_or_none()
    if row is None or row[0] is None:
        return None, None
    return float(row[0]), float(row[1])


async def list_regions(session: AsyncSession, *, country_code: str | None) -> list[RegionOut]:
    stmt: Select[tuple[Region]] = select(Region).where(Region.status == "active")
    if country_code:
        stmt = stmt.join(Country, Country.id == Region.country_id).where(
            Country.code == country_code.upper()
        )
    stmt = stmt.order_by(Region.name)
    regions = (await session.scalars(stmt)).all()

    out: list[RegionOut] = []
    for region in regions:
        lng, lat = await _coords_for_region(session, region.id)
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
    out: list[LocalityOut] = []
    for locality in localities:
        lng, lat = await _coords_for_locality(session, locality.id)
        payload = LocalityOut.model_validate(locality)
        out.append(payload.model_copy(update={"center_lng": lng, "center_lat": lat}))
    return out
