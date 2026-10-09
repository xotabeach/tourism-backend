"""Completing or cancelling a run, and the walker's difficulty answer."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.route_execution.application import antifraud_service
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
from tourism_backend.modules.route_execution.application.execution_reward import (
    _award_completion_points,
)
from tourism_backend.modules.route_execution.application.offline_sync import (
    resolve_event_time,
    terminal_conflict_details,
)
from tourism_backend.modules.route_execution.application.rewards import (
    completed_share_percent,
    run_counts,
)
from tourism_backend.modules.route_execution.application.schemas import (
    RouteExecutionEventIn,
    RouteExecutionOut,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionStop,
)


async def complete_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    event: RouteExecutionEventIn | None = None,
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

    user = await antifraud_service.lock_user(session, user_id)
    execution = await _owned_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
        for_update=True,
    )
    if execution.status == "completed":
        return await _execution_out(session, execution)
    if execution.status != "active":
        raise AppError(
            code="route_execution_not_active",
            message="Route execution is not active",
            status_code=409,
            details=terminal_conflict_details(execution.status),
        )
    incomplete = list(
        (
            await session.scalars(
                select(RouteExecutionStop)
                .where(
                    RouteExecutionStop.execution_id == execution.id,
                    RouteExecutionStop.is_optional.is_(False),
                    RouteExecutionStop.completed_at.is_(None),
                    # A skipped stop is settled: it no longer blocks the end.
                    RouteExecutionStop.skipped_at.is_(None),
                )
                .order_by(RouteExecutionStop.position)
            )
        ).all()
    )
    if incomplete:
        raise AppError(
            code="required_stops_incomplete",
            message="Complete all required stops first",
            status_code=409,
            details={
                "stop_ids": [str(stop.id) for stop in incomplete],
                "retryable": False,
            },
        )
    last_stop_at = await _latest_stop_completion(session, execution_id=execution.id)
    if last_stop_at is None:
        # Every stop skipped and none reached is not a walked route: the run
        # can only be cancelled.
        raise AppError(
            code="no_stops_marked",
            message="Mark at least one stop before completing the route",
            status_code=409,
            details={"retryable": False},
        )
    required = list(
        await session.scalars(
            select(RouteExecutionStop).where(
                RouteExecutionStop.execution_id == execution.id,
                RouteExecutionStop.is_optional.is_(False),
            )
        )
    )
    settings = await load_settings(session)
    share = completed_share_percent(
        sum(stop.completed_at is not None for stop in required), len(required)
    )
    resolved = resolve_event_time(
        event.occurred_at if event is not None else None,
        now=now,
        not_before=max(execution.started_at, last_stop_at or execution.started_at),
    )
    execution.status = "completed"
    execution.completed_at = resolved.effective
    # Fixed here and never recomputed (spec 15, D2, D16): the walker is paid
    # for what they walked, and the run counts as «прошёл маршрут» when the
    # share of marked required stops reaches the threshold of that moment.
    execution.completed_share_percent = share
    execution.counted = run_counts(share, settings.counted_stops_percent)
    execution.updated_at = now
    await _award_completion_points(
        session,
        execution=execution,
        user=user,
        settings=settings,
        now=now,
    )
    return await _commit_event(
        session,
        execution=execution,
        action="complete",
        resolved=resolved,
        now=now,
        applied=True,
        client_event_id=client_event_id,
    )


async def cancel_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    event: RouteExecutionEventIn | None = None,
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

    execution = await _owned_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
        for_update=True,
    )
    if execution.status == "cancelled":
        return await _execution_out(session, execution)
    # A paused run can still be abandoned outright — forcing a resume first
    # just to cancel is friction with no benefit.
    if execution.status not in ("active", "paused"):
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
    if execution.status == "paused" and execution.paused_at is not None:
        execution.paused_duration_seconds += int(
            (resolved.effective - execution.paused_at).total_seconds()
        )
        execution.paused_at = None
    execution.status = "cancelled"
    execution.cancelled_at = resolved.effective
    execution.counted = False
    execution.updated_at = now
    return await _commit_event(
        session,
        execution=execution,
        action="cancel",
        resolved=resolved,
        now=now,
        applied=True,
        client_event_id=client_event_id,
    )


async def record_difficulty_feedback(
    session: AsyncSession, *, user_id: UUID, execution_id: UUID, answer: str
) -> None:
    """Keep the walker's «легче / как ожидал / сложнее» (spec 17, section 7).

    Only for a run that is over; a second answer replaces the first.
    """
    execution = await session.get(RouteExecution, execution_id)
    if execution is None or execution.user_id != user_id:
        raise AppError(
            code="route_execution_not_found", message="Прохождение не найдено", status_code=404
        )
    if execution.status not in ("completed", "cancelled"):
        raise AppError(
            code="route_execution_not_finished",
            message="Оценить сложность можно после прохождения",
            status_code=409,
        )
    execution.difficulty_feedback = answer
    execution.difficulty_feedback_at = datetime.now(UTC)
    await session.commit()
