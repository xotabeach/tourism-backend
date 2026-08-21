"""HTTP API for route builder match + generate + AI chat sessions."""

from uuid import UUID

from fastapi import APIRouter, Query

from tourism_backend.api.deps import CurrentUserId, DbSession, SettingsDep
from tourism_backend.modules.route_builder.application import (
    generate_service,
    match_service,
    session_service,
)
from tourism_backend.modules.route_builder.application.schemas import (
    RouteGenerateIn,
    RouteGenerateOut,
    RouteMatchOut,
    RouteMatchParamsIn,
    RoutePlanningMessageIn,
    RoutePlanningMessageListOut,
    RoutePlanningMessageOut,
    RoutePlanningSessionCreateIn,
    RoutePlanningSessionListOut,
    RoutePlanningSessionOut,
    RouteProposalOut,
)

router = APIRouter(prefix="/route-builder", tags=["route-builder"])


@router.post("/match", response_model=RouteMatchOut)
async def match_routes(
    payload: RouteMatchParamsIn,
    session: DbSession,
    user_id: CurrentUserId,
    settings: SettingsDep,
) -> RouteMatchOut:
    return await match_service.match_routes(
        session,
        user_id=user_id,
        params=payload,
        ai_planning_enabled=settings.ai_planning_enabled,
    )


@router.post("/generate", response_model=RouteGenerateOut)
async def generate_route(
    payload: RouteGenerateIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteGenerateOut:
    return await generate_service.generate_route(
        session,
        user_id=user_id,
        payload=payload,
    )


@router.post("/proposals/{proposal_id}/accept", response_model=RouteProposalOut)
async def accept_proposal(
    proposal_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteProposalOut:
    return await generate_service.accept_proposal(
        session,
        user_id=user_id,
        proposal_id=proposal_id,
    )


@router.post("/proposals/{proposal_id}/reject", response_model=RouteProposalOut)
async def reject_proposal(
    proposal_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteProposalOut:
    return await generate_service.reject_proposal(
        session,
        user_id=user_id,
        proposal_id=proposal_id,
    )


@router.get("/sessions", response_model=RoutePlanningSessionListOut)
async def list_planning_sessions(
    session: DbSession,
    user_id: CurrentUserId,
    settings: SettingsDep,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RoutePlanningSessionListOut:
    return await session_service.list_sessions(
        session,
        user_id=user_id,
        limit=limit,
        offset=offset,
        settings=settings,
    )


@router.post("/sessions", response_model=RoutePlanningSessionOut)
async def create_planning_session(
    payload: RoutePlanningSessionCreateIn,
    session: DbSession,
    user_id: CurrentUserId,
    settings: SettingsDep,
) -> RoutePlanningSessionOut:
    return await session_service.create_session(
        session,
        user_id=user_id,
        payload=payload,
        settings=settings,
    )


@router.get("/sessions/{session_id}", response_model=RoutePlanningSessionOut)
async def get_planning_session(
    session_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    settings: SettingsDep,
) -> RoutePlanningSessionOut:
    return await session_service.get_session(
        session,
        user_id=user_id,
        session_id=session_id,
        settings=settings,
    )


@router.post("/sessions/{session_id}/close", response_model=RoutePlanningSessionOut)
async def close_planning_session(
    session_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    settings: SettingsDep,
) -> RoutePlanningSessionOut:
    return await session_service.close_session(
        session,
        user_id=user_id,
        session_id=session_id,
        settings=settings,
    )


@router.get(
    "/sessions/{session_id}/messages",
    response_model=RoutePlanningMessageListOut,
)
async def list_planning_messages(
    session_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    settings: SettingsDep,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RoutePlanningMessageListOut:
    return await session_service.list_messages(
        session,
        user_id=user_id,
        session_id=session_id,
        limit=limit,
        offset=offset,
        settings=settings,
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=RoutePlanningMessageOut,
)
async def post_planning_message(
    session_id: UUID,
    payload: RoutePlanningMessageIn,
    session: DbSession,
    user_id: CurrentUserId,
    settings: SettingsDep,
) -> RoutePlanningMessageOut:
    return await session_service.post_message(
        session,
        user_id=user_id,
        session_id=session_id,
        payload=payload,
        settings=settings,
    )
