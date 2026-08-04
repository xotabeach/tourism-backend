from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.routes.application import media as route_media
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.schemas import (
    RouteDetailOut,
    RouteListOut,
    UserRouteDraftIn,
    UserRouteDraftOut,
    UserRouteMediaOut,
)

router = APIRouter(tags=["routes"])


@router.post("/routes/drafts", response_model=UserRouteDraftOut)
async def save_route_draft(
    payload: UserRouteDraftIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> UserRouteDraftOut:
    return await routes_service.save_user_route_draft(
        session,
        owner_user_id=user_id,
        payload=payload,
    )


@router.delete(
    "/routes/drafts/{route_id}/media",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_route_draft_media(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await routes_service.clear_user_route_media(
        session,
        route_id=route_id,
        owner_user_id=user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/routes/drafts/{route_id}/media",
    response_model=UserRouteMediaOut,
)
async def upload_route_draft_media(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
    position: Annotated[int, Form(ge=0, le=9)],
) -> UserRouteMediaOut:
    # Authorize before reading/writing an attacker-controlled upload.
    await routes_service.ensure_user_route_editable(
        session,
        route_id=route_id,
        owner_user_id=user_id,
    )
    saved = await route_media.save_route_media(file, route_id=route_id)
    return await routes_service.add_user_route_media(
        session,
        route_id=route_id,
        owner_user_id=user_id,
        position=position,
        saved=saved,
    )


@router.post("/routes/{route_id}/submit", response_model=UserRouteDraftOut)
async def submit_route(
    route_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> UserRouteDraftOut:
    return await routes_service.submit_user_route(
        session,
        route_id=route_id,
        owner_user_id=user_id,
    )


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
