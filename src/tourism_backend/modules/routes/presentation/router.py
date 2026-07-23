from uuid import UUID

from fastapi import APIRouter, Query

from tourism_backend.api.deps import DbSession
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.schemas import RouteDetailOut, RouteListOut

router = APIRouter(tags=["routes"])


@router.get("/routes", response_model=RouteListOut)
async def get_routes(
    session: DbSession,
    region_slug: str | None = Query(default=None, max_length=128),
    transport_mode: str | None = Query(default=None, max_length=32),
    difficulty: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RouteListOut:
    return await routes_service.list_routes(
        session,
        region_slug=region_slug,
        transport_mode=transport_mode,
        difficulty=difficulty,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/routes/{route_id}", response_model=RouteDetailOut)
async def get_route(session: DbSession, route_id: UUID) -> RouteDetailOut:
    return await routes_service.get_route(session, route_id)
