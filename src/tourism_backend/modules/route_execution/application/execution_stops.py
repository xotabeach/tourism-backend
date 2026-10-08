"""Marking a stop and taking the mark back."""

import math
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.route_execution.application import antifraud_service
from tourism_backend.modules.route_execution.application.antifraud_logic import (
    GpsReading,
    haversine_meters,
)
from tourism_backend.modules.route_execution.application.antifraud_settings import (
    load_settings,
)
from tourism_backend.modules.route_execution.application.execution_common import (
    _commit_event,
    _execution_out,
    _latest_stop_completion,
    _owned_execution,
    _replayed_out,
)
from tourism_backend.modules.route_execution.application.offline_sync import (
    resolve_event_time,
    terminal_conflict_details,
)
from tourism_backend.modules.route_execution.application.schemas import (
    RouteExecutionEventIn,
    RouteExecutionOut,
    RouteExecutionStopMarkIn,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecutionStop,
)


async def complete_stop(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    stop_id: UUID,
    event: RouteExecutionStopMarkIn | None = None,
) -> RouteExecutionOut:
    now = datetime.now(UTC)
    client_event_id = event.client_event_id if event is not None else None
    if client_event_id is not None:
        replayed = await _replayed_out(
            session,
            user_id=user_id,
            execution_id=execution_id,
            client_event_id=client_event_id,
        )
        if replayed is not None:
            return replayed

    # One user's marks are judged one at a time: violation counts, flags and
    # points must not race with a parallel mark or an offline sync batch.
    await antifraud_service.lock_user(session, user_id)
    execution = await _owned_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
        for_update=True,
    )
    stop = await session.scalar(
        select(RouteExecutionStop).where(
            RouteExecutionStop.id == stop_id,
            RouteExecutionStop.execution_id == execution.id,
        )
    )
    if stop is None:
        raise AppError(
            code="route_execution_stop_not_found",
            message="Route execution stop not found",
            status_code=404,
        )
    if execution.status != "active":
        # A queued action for a stop the run already recorded is not an error;
        # anything else cannot be applied to a finished run.
        if stop.completed_at is not None:
            return await _execution_out(session, execution)
        raise AppError(
            code="route_execution_not_active",
            message="Route execution is not active",
            status_code=409,
            details=terminal_conflict_details(execution.status),
        )

    resolved = resolve_event_time(
        event.occurred_at if event is not None else None,
        now=now,
        not_before=execution.started_at,
    )
    applied = stop.completed_at is None
    assessment = antifraud_service.MarkAssessment()
    if applied:
        stop.completed_at = resolved.effective
        stop.updated_at = now
        execution.updated_at = now
        position = event.position if event is not None else None
        if position is not None and stop.lat is not None and stop.lng is not None:
            stop.device_distance_m = math.ceil(
                haversine_meters(position.lat, position.lng, stop.lat, stop.lng)
            )
        assessment = await antifraud_service.assess_stop_mark(
            session,
            execution=execution,
            stop=stop,
            effective_at=resolved.effective,
            reported_at=resolved.reported,
            position=(
                GpsReading(lat=position.lat, lng=position.lng, accuracy_m=position.accuracy_m)
                if position is not None
                else None
            ),
            settings=await load_settings(session),
            now=now,
        )
    out = await _commit_event(
        session,
        execution=execution,
        action="complete_stop",
        resolved=resolved,
        now=now,
        applied=applied,
        stop_id=stop.id,
        client_event_id=client_event_id,
        pace_verdict=assessment.pace_verdict if applied else None,
    )
    await antifraud_service.deliver_pushes(session, assessment.pushes)
    return out


async def uncomplete_stop(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    stop_id: UUID,
    event: RouteExecutionEventIn | None = None,
) -> RouteExecutionOut:
    """Take back the latest mark of a run in progress (FRONTEND-36).

    Only the most recent mark can be undone, so legs and pace keep following
    the order the stops were really reached in. The anti-fraud record of the
    undone mark stays in the journal: marking, unmarking and marking again
    must not wash a violation out. A repeated mark is judged afresh.
    """
    now = datetime.now(UTC)
    client_event_id = event.client_event_id if event is not None else None
    if client_event_id is not None:
        replayed = await _replayed_out(
            session,
            user_id=user_id,
            execution_id=execution_id,
            client_event_id=client_event_id,
        )
        if replayed is not None:
            return replayed

    await antifraud_service.lock_user(session, user_id)
    execution = await _owned_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
        for_update=True,
    )
    stop = await session.scalar(
        select(RouteExecutionStop).where(
            RouteExecutionStop.id == stop_id,
            RouteExecutionStop.execution_id == execution.id,
        )
    )
    if stop is None:
        raise AppError(
            code="route_execution_stop_not_found",
            message="Route execution stop not found",
            status_code=404,
        )
    if stop.completed_at is None:
        # Already unmarked (a retried request): nothing left to undo.
        return await _execution_out(session, execution)
    if execution.status not in {"active", "paused"}:
        raise AppError(
            code="route_execution_not_active",
            message="Route execution is not active",
            status_code=409,
            details=terminal_conflict_details(execution.status),
        )
    last_stop_at = await _latest_stop_completion(session, execution_id=execution.id)
    if last_stop_at is not None and stop.completed_at < last_stop_at:
        raise AppError(
            code="route_execution_stop_not_last",
            message="Only the latest marked stop can be unmarked",
            status_code=409,
            details={"retryable": False},
        )

    resolved = resolve_event_time(
        event.occurred_at if event is not None else None,
        now=now,
        not_before=stop.completed_at,
    )
    stop.completed_at = None
    stop.mark_below_floor = False
    stop.updated_at = now
    execution.updated_at = now
    return await _commit_event(
        session,
        execution=execution,
        action="uncomplete_stop",
        resolved=resolved,
        now=now,
        applied=True,
        stop_id=stop.id,
        client_event_id=client_event_id,
    )
