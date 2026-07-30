"""Public profile read APIs (no phone / no mutations)."""

from uuid import UUID

from fastapi import APIRouter, Query

from tourism_backend.api.deps import DbSession
from tourism_backend.modules.identity.application import public_service
from tourism_backend.modules.identity.application.public_schemas import PublicUserOut
from tourism_backend.modules.routes.application.schemas import RouteListOut

router = APIRouter(tags=["users"])


@router.get("/users/{user_id}", response_model=PublicUserOut)
async def get_public_user(session: DbSession, user_id: UUID) -> PublicUserOut:
    return await public_service.get_public_user(session, user_id)


@router.get("/users/{user_id}/routes", response_model=RouteListOut)
async def get_public_user_routes(
    session: DbSession,
    user_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RouteListOut:
    return await public_service.list_public_user_routes(
        session,
        user_id,
        limit=limit,
        offset=offset,
    )
