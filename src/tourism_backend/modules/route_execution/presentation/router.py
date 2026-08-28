"""Authenticated route execution endpoints."""

from uuid import UUID

from fastapi import APIRouter, Query, status

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.route_execution.application import service
from tourism_backend.modules.route_execution.application.schemas import (
    RouteExecutionListOut,
    RouteExecutionOut,
    RouteExecutionStartIn,
)

router = APIRouter(prefix="/route-executions", tags=["route-executions"])


@router.post("", response_model=RouteExecutionOut, status_code=status.HTTP_201_CREATED)
async def start_route_execution(
    payload: RouteExecutionStartIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteExecutionOut:
    return await service.start_execution(
        session,
        user_id=user_id,
        route_id=payload.route_id,
    )


@router.get("", response_model=RouteExecutionListOut)
async def list_route_executions(
    session: DbSession,
    user_id: CurrentUserId,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> RouteExecutionListOut:
    return await service.list_executions(
        session,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )


@router.get("/active", response_model=RouteExecutionOut | None)
async def get_active_route_execution(
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteExecutionOut | None:
    return await service.get_active_execution(session, user_id=user_id)


@router.get("/{execution_id}", response_model=RouteExecutionOut)
async def get_route_execution(
    execution_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteExecutionOut:
    return await service.get_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
    )


@router.put("/{execution_id}/stops/{stop_id}/complete", response_model=RouteExecutionOut)
async def complete_route_execution_stop(
    execution_id: UUID,
    stop_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteExecutionOut:
    return await service.complete_stop(
        session,
        user_id=user_id,
        execution_id=execution_id,
        stop_id=stop_id,
    )


@router.post("/{execution_id}/complete", response_model=RouteExecutionOut)
async def complete_route_execution(
    execution_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteExecutionOut:
    return await service.complete_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
    )


@router.post("/{execution_id}/cancel", response_model=RouteExecutionOut)
async def cancel_route_execution(
    execution_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> RouteExecutionOut:
    return await service.cancel_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
    )
