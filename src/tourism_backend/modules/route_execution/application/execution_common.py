"""Shared pieces of a route run: access checks, the response shape and the idempotent event log."""

from datetime import datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tourism_backend.api.errors import AppError
from tourism_backend.modules.achievements.service import after_commit as evaluate_achievements
from tourism_backend.modules.route_execution.application import antifraud_service
from tourism_backend.modules.route_execution.application.antifraud_logic import (
    pace_warn_below_seconds,
)
from tourism_backend.modules.route_execution.application.antifraud_settings import (
    load_settings,
)
from tourism_backend.modules.route_execution.application.offline_sync import (
    EventAction,
    ResolvedEventTime,
)
from tourism_backend.modules.route_execution.application.routing_snapshot import (
    routing_snapshot_out,
)
from tourism_backend.modules.route_execution.application.schemas import (
    AntiFraudOut,
    PaceVerdictOut,
    PointsStatus,
    RouteExecutionOut,
    RouteExecutionStatus,
    RouteExecutionStopOut,
    RouteExecutionSyncOut,
    StopSkipReason,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionEvent,
    RouteExecutionStop,
    RoutingSnapshotDay,
)
from tourism_backend.modules.routes.application.service import route_cover_urls
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview

_PUBLIC_ROUTE = and_(
    Route.source.in_(("editorial", "user_created")),
    Route.visibility == "public",
    Route.lifecycle_status == "active",
    Route.publication_status == "published",
)


def _owned_route(user_id: UUID) -> ColumnElement[bool]:
    return and_(
        Route.owner_user_id == user_id,
        Route.source.in_(("generated", "user_created")),
        Route.publication_status != "deleted",
        # An edit of a published route is not a route to walk.
        Route.revision_of_route_id.is_(None),
    )


async def _owned_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    for_update: bool = False,
) -> RouteExecution:
    stmt: Select[tuple[RouteExecution]] = select(RouteExecution).where(
        RouteExecution.id == execution_id,
        RouteExecution.user_id == user_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    execution = await session.scalar(stmt)
    if execution is None:
        raise AppError(
            code="route_execution_not_found",
            message="Route execution not found",
            status_code=404,
        )
    return execution


async def _execution_out(
    session: AsyncSession,
    execution: RouteExecution,
    sync: RouteExecutionSyncOut | None = None,
    pace_verdict: PaceVerdictOut | None = None,
) -> RouteExecutionOut:
    stops = list(
        (
            await session.scalars(
                select(RouteExecutionStop)
                .where(RouteExecutionStop.execution_id == execution.id)
                .order_by(RouteExecutionStop.position)
            )
        ).all()
    )
    completed = sum(stop.completed_at is not None for stop in stops)
    required = [stop for stop in stops if not stop.is_optional]
    completed_required = sum(stop.completed_at is not None for stop in required)
    skipped_required = sum(stop.skipped_at is not None for stop in required)
    routing = await routing_snapshot_out(session, execution.routing_snapshot_id)
    antifraud_settings = await load_settings(session)
    hints_enabled = False
    if antifraud_settings.enforcing:
        state = await antifraud_service.get_state(session, execution.user_id)
        hints_enabled = state is None or not state.is_trusted
    first_mark = completed == 0
    last_resume = await session.scalar(
        select(func.max(RouteExecutionEvent.effective_at)).where(
            RouteExecutionEvent.execution_id == execution.id,
            RouteExecutionEvent.action == "resume",
            RouteExecutionEvent.applied.is_(True),
        )
    )
    last_activity = max(
        moment
        for moment in (
            execution.started_at,
            last_resume,
            *(stop.completed_at for stop in stops),
        )
        if moment is not None
    )
    my_review_exists = False
    if execution.status == "completed" and execution.route_id is not None:
        my_review_exists = bool(
            await session.scalar(
                select(
                    exists().where(
                        RouteReview.route_id == execution.route_id,
                        RouteReview.author_user_id == execution.user_id,
                        RouteReview.reply_to_review_id.is_(None),
                        RouteReview.status != "deleted",
                    )
                )
            )
        )
    cover_url = execution.route_cover_url
    if cover_url is None and execution.route_id is not None:
        # Runs started before FRONTEND-42 saved no cover when the route had no
        # own one; show the catalog's instead of a grey card.
        cover_url = (await route_cover_urls(session, [execution.route_id])).get(execution.route_id)
    planned_days = len(await _snapshot_days(session, execution)) or 1
    return RouteExecutionOut(
        planned_days=planned_days,
        current_day=execution.night_pauses + 1,
        night_paused=execution.night_paused,
        ended_early=execution.ended_early,
        id=execution.id,
        route_id=execution.route_id,
        route_name=execution.route_name,
        route_cover_url=cover_url,
        status=cast(RouteExecutionStatus, execution.status),
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        cancelled_at=execution.cancelled_at,
        routing=routing,
        total_stops=len(stops),
        completed_stops=completed,
        required_stops=len(required),
        completed_required_stops=completed_required,
        skipped_required_stops=skipped_required,
        counted=execution.counted,
        completed_share_percent=execution.completed_share_percent,
        counted_threshold_percent=antifraud_settings.counted_stops_percent,
        stops=[
            RouteExecutionStopOut(
                id=stop.id,
                route_stop_id=stop.route_stop_id,
                place_id=stop.place_id,
                position=stop.position,
                place_name=stop.place_name,
                lat=stop.lat,
                lng=stop.lng,
                is_optional=stop.is_optional,
                completed_at=stop.completed_at,
                skipped_at=stop.skipped_at,
                skip_reason=cast(StopSkipReason | None, stop.skip_reason),
                leg_distance_meters=stop.leg_distance_meters,
                leg_estimate_seconds=stop.leg_estimate_seconds,
                leg_estimate_source=cast(
                    Literal["provider", "straight_line"] | None,
                    stop.leg_estimate_source,
                ),
                pace_warn_below_seconds=(
                    pace_warn_below_seconds(
                        estimate_seconds=antifraud_service.pace_estimate(stop),
                        is_first_mark=first_mark,
                        rules=antifraud_settings.pace,
                    )
                    if hints_enabled and stop.completed_at is None
                    else None
                ),
            )
            for stop in stops
        ],
        awarded_points=int(execution.awarded_points or 0),
        points_status=cast(PointsStatus, execution.points_status),
        points_reason=execution.points_reason,
        held_points=(
            int(execution.computed_points or 0) if execution.points_status == "held" else 0
        ),
        antifraud=(
            AntiFraudOut(
                gps_tolerance_m=antifraud_settings.gps.tolerance_meters,
                gps_min_accuracy_m=antifraud_settings.gps.min_accuracy_meters,
                route_cooldown_days=antifraud_settings.route_points_cooldown_days,
                daily_points_cap=antifraud_settings.daily_points_cap,
            )
            if hints_enabled
            else None
        ),
        pace_verdict=pace_verdict,
        paused_duration_seconds=int(execution.paused_duration_seconds or 0),
        paused_at=execution.paused_at if execution.status == "paused" else None,
        last_activity_at=last_activity,
        my_review_exists=my_review_exists,
        sync=sync,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )


def _sync_out(event: RouteExecutionEvent, *, replayed: bool) -> RouteExecutionSyncOut:
    return RouteExecutionSyncOut(
        action=cast(EventAction, event.action),
        client_event_id=event.client_event_id,
        occurred_at=event.occurred_at,
        effective_at=event.effective_at,
        recorded_at=event.recorded_at,
        replayed=replayed,
        applied=event.applied,
    )


async def _event_by_client_id(
    session: AsyncSession,
    *,
    user_id: UUID,
    client_event_id: UUID,
) -> RouteExecutionEvent | None:
    event: RouteExecutionEvent | None = await session.scalar(
        select(RouteExecutionEvent).where(
            RouteExecutionEvent.user_id == user_id,
            RouteExecutionEvent.client_event_id == client_event_id,
        )
    )
    return event


async def _replayed_out(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
    client_event_id: UUID,
) -> RouteExecutionOut | None:
    """Answer an already-recorded event with current state, never a conflict."""

    recorded = await _event_by_client_id(
        session,
        user_id=user_id,
        client_event_id=client_event_id,
    )
    if recorded is None:
        return None
    execution = await _owned_execution(session, user_id=user_id, execution_id=execution_id)
    return await _execution_out(session, execution, _sync_out(recorded, replayed=True))


async def _commit_event(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    action: EventAction,
    resolved: ResolvedEventTime,
    now: datetime,
    applied: bool,
    stop_id: UUID | None = None,
    client_event_id: UUID | None = None,
    pace_verdict: PaceVerdictOut | None = None,
) -> RouteExecutionOut:
    event = RouteExecutionEvent(
        id=uuid4(),
        execution_id=execution.id,
        user_id=execution.user_id,
        stop_id=stop_id,
        action=action,
        client_event_id=client_event_id,
        occurred_at=resolved.reported,
        effective_at=resolved.effective,
        recorded_at=now,
        applied=applied,
    )
    session.add(event)
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent delivery of the same queued action won the race.
        await session.rollback()
        if client_event_id is None:
            raise
        replayed = await _replayed_out(
            session,
            user_id=execution.user_id,
            execution_id=execution.id,
            client_event_id=client_event_id,
        )
        if replayed is None:
            raise
        return replayed
    if applied and action in {"complete_stop", "complete"}:
        await evaluate_achievements(
            session, execution.user_id, {"stop" if action == "complete_stop" else "completion"}
        )
    return await _execution_out(
        session,
        execution,
        _sync_out(event, replayed=False),
        pace_verdict,
    )


async def _latest_stop_completion(
    session: AsyncSession,
    *,
    execution_id: UUID,
) -> datetime | None:
    return await session.scalar(
        select(func.max(RouteExecutionStop.completed_at)).where(
            RouteExecutionStop.execution_id == execution_id
        )
    )


async def _snapshot_days(session: AsyncSession, execution: RouteExecution) -> list[tuple[int, int]]:
    """(first, last) stop positions of each planned day, in order."""
    if execution.routing_snapshot_id is None:
        return []
    rows = await session.execute(
        select(RoutingSnapshotDay.first_position, RoutingSnapshotDay.last_position)
        .where(RoutingSnapshotDay.snapshot_id == execution.routing_snapshot_id)
        .order_by(RoutingSnapshotDay.day_index)
    )
    return [(int(first), int(last)) for first, last in rows]
