"""Pauses, days of a multi-day run, finishing early and closing an idle run."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.route_execution.application import antifraud_service
from tourism_backend.modules.route_execution.application.execution_common import (
    _commit_event,
    _execution_out,
    _latest_stop_completion,
    _owned_execution,
    _replayed_out,
    _snapshot_days,
)
from tourism_backend.modules.route_execution.application.execution_reward import (
    _end_early,
)
from tourism_backend.modules.route_execution.application.offline_sync import (
    resolve_event_time,
    terminal_conflict_details,
)
from tourism_backend.modules.route_execution.application.schemas import (
    RouteExecutionEventIn,
    RouteExecutionOut,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
)


async def pause_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    event: RouteExecutionEventIn | None = None,
    night: bool = False,
) -> RouteExecutionOut:
    """Pause a run; ``night`` is «Закончить день» of a multi-day run (spec 14a).

    Only the walker ends a day: a pause put in by the server at sunset
    would take walking time off the leg and read as «too fast» (D21).
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

    execution = await _owned_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
        for_update=True,
    )
    if execution.status == "paused":
        return await _execution_out(session, execution)
    if execution.status != "active":
        raise AppError(
            code="route_execution_not_active",
            message="Route execution is not active",
            status_code=409,
            details=terminal_conflict_details(execution.status),
        )
    last_stop_at = await _latest_stop_completion(session, execution_id=execution.id)
    resolved = resolve_event_time(
        event.occurred_at if event is not None else None,
        now=now,
        not_before=max(execution.started_at, last_stop_at or execution.started_at),
    )
    execution.status = "paused"
    execution.paused_at = resolved.effective
    execution.updated_at = now
    if night:
        execution.night_paused = True
        execution.night_pauses += 1
    return await _commit_event(
        session,
        execution=execution,
        action="end_day" if night else "pause",
        resolved=resolved,
        now=now,
        applied=True,
        client_event_id=client_event_id,
    )


async def resume_execution(
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
    if execution.status == "active":
        return await _execution_out(session, execution)
    if execution.status != "paused":
        raise AppError(
            code="route_execution_not_active",
            message="Route execution is not active",
            status_code=409,
            details=terminal_conflict_details(execution.status),
        )
    resolved = resolve_event_time(
        event.occurred_at if event is not None else None,
        now=now,
        # A resume can never precede the pause it closes out.
        not_before=execution.paused_at or execution.started_at,
    )
    if execution.paused_at is not None:
        execution.paused_duration_seconds += int(
            (resolved.effective - execution.paused_at).total_seconds()
        )
    execution.paused_at = None
    execution.night_paused = False
    execution.status = "active"
    execution.updated_at = now
    return await _commit_event(
        session,
        execution=execution,
        action="resume",
        resolved=resolved,
        now=now,
        applied=True,
        client_event_id=client_event_id,
    )


# ---------------------------------------------------------- multi-day runs

#: A multi-day run with no event for this long is closed (spec 14, D21):
#: max(planned days + 3, 7) days.
_IDLE_MIN_DAYS = 7


_IDLE_EXTRA_DAYS = 3


async def finish_early_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    event: RouteExecutionEventIn | None = None,
) -> RouteExecutionOut:
    """«Завершить многодневный маршрут»: stop here, keep the finished days.

    No «прошёл маршрут» achievement or walker's review: the route was not
    walked to the end (spec 14, D21).
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
    user = await antifraud_service.lock_user(session, user_id)
    execution = await _owned_execution(
        session, user_id=user_id, execution_id=execution_id, for_update=True
    )
    if execution.status == "cancelled" and execution.ended_early:
        return await _execution_out(session, execution)
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
        not_before=execution.paused_at or execution.started_at,
    )
    await _end_early(session, execution=execution, user=user, moment=resolved.effective, now=now)
    return await _commit_event(
        session,
        execution=execution,
        action="finish_early",
        resolved=resolved,
        now=now,
        applied=True,
        client_event_id=client_event_id,
    )


async def _close_if_idle(
    session: AsyncSession, execution: RouteExecution, *, now: datetime
) -> bool:
    """Close a multi-day run nobody came back to; True when it was closed.

    Done lazily when its owner next asks for the active run or starts a
    route, so an abandoned run never blocks them and no job is needed.
    """
    planned = len(await _snapshot_days(session, execution))
    if planned <= 1 and execution.night_pauses == 0:
        return False
    idle_days = max(planned + _IDLE_EXTRA_DAYS, _IDLE_MIN_DAYS)
    if now - execution.updated_at < timedelta(days=idle_days):
        return False
    user = await antifraud_service.lock_user(session, execution.user_id)
    moment = execution.paused_at or execution.updated_at
    await _end_early(session, execution=execution, user=user, moment=moment, now=now)
    await _commit_event(
        session,
        execution=execution,
        action="finish_early",
        resolved=resolve_event_time(None, now=now, not_before=moment),
        now=now,
        applied=True,
    )
    return True
