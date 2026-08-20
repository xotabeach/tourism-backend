"""HTTP API for route builder match + generate (Phase 8A)."""

from uuid import UUID

from fastapi import APIRouter

from tourism_backend.api.deps import CurrentUserId, DbSession, SettingsDep
from tourism_backend.modules.route_builder.application import generate_service, match_service
from tourism_backend.modules.route_builder.application.schemas import (
    RouteGenerateIn,
    RouteGenerateOut,
    RouteMatchOut,
    RouteMatchParamsIn,
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
