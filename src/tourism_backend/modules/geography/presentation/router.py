from fastapi import APIRouter, Query

from tourism_backend.api.deps import DbSession
from tourism_backend.modules.geography.application import service as geography_service
from tourism_backend.modules.geography.application.schemas import (
    CountryOut,
    LocalityOut,
    RegionOut,
)

router = APIRouter(prefix="/geography", tags=["geography"])


@router.get("/countries", response_model=list[CountryOut])
async def get_countries(session: DbSession) -> list[CountryOut]:
    return await geography_service.list_countries(session)


@router.get("/regions", response_model=list[RegionOut])
async def get_regions(
    session: DbSession,
    country_code: str | None = Query(default=None, max_length=8),
) -> list[RegionOut]:
    return await geography_service.list_regions(session, country_code=country_code)


@router.get("/localities", response_model=list[LocalityOut])
async def get_localities(
    session: DbSession,
    region_slug: str = Query(..., max_length=128),
) -> list[LocalityOut]:
    return await geography_service.list_localities(session, region_slug=region_slug)
