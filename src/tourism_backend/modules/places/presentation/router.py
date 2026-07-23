from uuid import UUID

from fastapi import APIRouter, Query

from tourism_backend.api.deps import DbSession
from tourism_backend.modules.places.application import service as places_service
from tourism_backend.modules.places.application.schemas import (
    CategoryOut,
    PlaceDetailOut,
    PlaceListOut,
)

router = APIRouter(tags=["places"])


@router.get("/categories", response_model=list[CategoryOut])
async def get_categories(session: DbSession) -> list[CategoryOut]:
    return await places_service.list_categories(session)


@router.get("/places", response_model=PlaceListOut)
async def get_places(
    session: DbSession,
    region_slug: str | None = Query(default=None),
    locality_slug: str | None = Query(default=None),
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PlaceListOut:
    return await places_service.list_places(
        session,
        region_slug=region_slug,
        locality_slug=locality_slug,
        category=category,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/places/{place_id}", response_model=PlaceDetailOut)
async def get_place(session: DbSession, place_id: UUID) -> PlaceDetailOut:
    return await places_service.get_place(session, place_id)
