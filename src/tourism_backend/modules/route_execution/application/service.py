"""Route execution state machine and ownership rules."""

import math
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tourism_backend.api.errors import AppError
from tourism_backend.modules.achievements.service import after_commit as evaluate_achievements
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure.models import Place, RoadEvent
from tourism_backend.modules.route_builder.application.route_quality import (
    RoadEventSignal,
    active_road_event_blockers,
)
from tourism_backend.modules.route_builder.application.routing import normalize_transport_mode
from tourism_backend.modules.route_execution.application import antifraud_service
from tourism_backend.modules.route_execution.application.antifraud_logic import (
    GpsReading,
    StopPoint,
    build_leg_estimates,
    haversine_meters,
    pace_warn_below_seconds,
    provider_legs_from_metadata,
)
from tourism_backend.modules.route_execution.application.antifraud_settings import (
    AntiFraudSettings,
    load_settings,
)
from tourism_backend.modules.route_execution.application.offline_sync import (
    EventAction,
    ResolvedEventTime,
    resolve_event_time,
    terminal_conflict_details,
)
from tourism_backend.modules.route_execution.application.rewards import (
    RouteEffort,
    SegmentEffort,
    travel_points_for_effort,
)
from tourism_backend.modules.route_execution.application.routing_snapshot import (
    ensure_routing_snapshot,
    routing_snapshot_out,
)
from tourism_backend.modules.route_execution.application.schemas import (
    AntiFraudOut,
    PaceVerdictOut,
    PointsStatus,
    RouteExecutionEventIn,
    RouteExecutionListOut,
    RouteExecutionOut,
    RouteExecutionStatus,
    RouteExecutionStopMarkIn,
    RouteExecutionStopOut,
    RouteExecutionSyncOut,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionEvent,
    RouteExecutionStop,
    RouteRoutingSnapshot,
    RoutingSnapshotDay,
    RoutingSnapshotSegment,
)
from tourism_backend.modules.routes.application.service import route_cover_urls
from tourism_backend.modules.routes.application.structure import refresh_route_structure
from tourism_backend.modules.routes.application.structure_rules import segment_mode_for
from tourism_backend.modules.routes.infrastructure.models import Route, RouteReview, RouteStop

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


async def start_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    route_id: UUID,
) -> RouteExecutionOut:
    # The user-row lock serializes double taps even before the partial unique
    # index has a row to protect.
    user_exists = await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
    if user_exists is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)

    active = await session.scalar(
        select(RouteExecution).where(
            RouteExecution.user_id == user_id,
            # A paused run is still "the one you're on" — it must keep
            # blocking a second start the same way an active run does.
            RouteExecution.status.in_(("active", "paused")),
        )
    )
    if active is not None and await _close_if_idle(session, active, now=datetime.now(UTC)):
        # Closing committed and let go of the user row: take it again.
        await session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        active = None
    if active is not None:
        if active.route_id == route_id:
            return await _execution_out(session, active)
        raise AppError(
            code="active_route_execution_exists",
            message="Finish or cancel the active route first",
            status_code=409,
            # Lets the app name the run in the way instead of a bare refusal.
            details={
                "execution_id": str(active.id),
                "route_id": str(active.route_id) if active.route_id else None,
                "route_name": active.route_name,
                "status": active.status,
                # Resting overnight in a multi-day run: the app offers
                # «Завершить многодневный маршрут» (spec 14a).
                "night_paused": active.night_paused,
            },
        )

    antifraud_settings = await load_settings(session)
    await antifraud_service.assert_start_allowed(
        session,
        user_id=user_id,
        settings=antifraud_settings,
        now=datetime.now(UTC),
    )

    route = await session.scalar(
        select(Route)
        .where(Route.id == route_id, or_(_PUBLIC_ROUTE, _owned_route(user_id)))
        .with_for_update()
    )
    if route is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)

    # Do not start a route that was explicitly marked unusable by the quality
    # gate. Older routes may have no routing metadata; those remain readable
    # for backwards compatibility and carry an honest ``unknown`` snapshot.
    routing_metadata = (
        route.accessibility.get("routing") if isinstance(route.accessibility, dict) else None
    )
    if isinstance(routing_metadata, dict) and routing_metadata.get("quality_status") == "unusable":
        raise AppError(
            code="route_quality_unusable",
            message="Маршрут нельзя начать: качество маршрута не подтверждено",
            status_code=409,
        )

    # Road events are region-level until segment geometry is available. An
    # active closure is therefore a conservative execution blocker; the
    # snapshot still records any earlier review warnings for observability.
    event_rows = list(
        (
            await session.scalars(
                select(RoadEvent)
                .where(
                    RoadEvent.region_id == route.region_id,
                    RoadEvent.status.in_(("active", "scheduled")),
                )
                .order_by(RoadEvent.starts_at, RoadEvent.id)
                .limit(64)
            )
        ).all()
    )
    blockers = active_road_event_blockers(
        tuple(
            RoadEventSignal(
                status=event.status,
                event_kind=event.event_kind,
                affects_transport=tuple((event.affects_transport or [])[:8]),
                starts_at=event.starts_at,
                ends_at=event.ends_at,
            )
            for event in event_rows
        ),
        transport_mode=normalize_transport_mode(route.transport_mode),
    )
    if blockers:
        raise AppError(
            code="route_blocked_by_road_event",
            message="Маршрут временно недоступен из-за дорожного ограничения",
            status_code=409,
            details={"reasons": list(blockers)},
        )

    rows = (
        await session.execute(
            select(
                RouteStop,
                Place,
                ST_X(sa_cast(Place.location, Geometry)),
                ST_Y(sa_cast(Place.location, Geometry)),
            )
            .join(Place, Place.id == RouteStop.place_id)
            .where(RouteStop.route_id == route.id)
            .order_by(RouteStop.position)
        )
    ).all()
    if not rows:
        raise AppError(
            code="route_has_no_stops",
            message="Route has no stops",
            status_code=409,
        )

    structure = await refresh_route_structure(session, route)
    routing_snapshot = await ensure_routing_snapshot(
        session,
        route=route,
        segments=structure.segments,
        days=structure.days,
        auto_day_count=structure.auto_day_count,
        stop_signature=[
            (route_stop.id, route_stop.position, place.id) for route_stop, place, _lng, _lat in rows
        ],
        captured_at=datetime.now(UTC),
    )

    # Same cover as the catalog card: the route's own cover, else the photo
    # of its first stop that has one. The own cover alone left most runs
    # without a picture (FRONTEND-42).
    cover_url = (await route_cover_urls(session, [route.id])).get(route.id)
    stop_points = [
        StopPoint(route_stop.position, lat, lng) for route_stop, _place, lng, lat in rows
    ]
    # Shown to the walker: the router's real legs when the route has them.
    # Straight-line speeds follow the mode the legs are travelled in: a
    # mixed route is driven until 14b, not walked (spec 14, step 0).
    leg_mode = segment_mode_for(route.base_mode)
    leg_estimates = build_leg_estimates(
        stop_points,
        transport_mode=leg_mode,
        provider_legs=provider_legs_from_metadata(
            routing_metadata if isinstance(routing_metadata, dict) else None,
            stop_count=len(rows),
        ),
    )
    # Judged by the pace check: the straight line until af_pace_source says
    # otherwise; router legs are compared in the log meanwhile (spec 12a, D4).
    pace_by_router = (await load_settings(session)).pace_source == "provider"
    pace_estimates = (
        leg_estimates
        if pace_by_router
        else build_leg_estimates(stop_points, transport_mode=leg_mode)
    )
    now = datetime.now(UTC)
    execution = RouteExecution(
        id=uuid4(),
        user_id=user_id,
        route_id=route.id,
        routing_snapshot_id=routing_snapshot.id,
        route_name=route.name,
        route_cover_url=cover_url,
        status="active",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(execution)
    await session.flush()
    session.add_all(
        [
            RouteExecutionStop(
                id=uuid4(),
                execution_id=execution.id,
                route_stop_id=route_stop.id,
                place_id=place.id,
                position=route_stop.position,
                place_name=place.name,
                lat=float(lat) if lat is not None else None,
                lng=float(lng) if lng is not None else None,
                is_optional=route_stop.is_optional,
                completed_at=None,
                leg_distance_meters=leg.distance_meters if leg else None,
                leg_estimate_seconds=leg.duration_seconds if leg else None,
                leg_estimate_source=leg.source if leg else None,
                pace_estimate_seconds=pace.duration_seconds if pace else None,
                created_at=now,
                updated_at=now,
            )
            for (route_stop, place, lng, lat), leg, pace in zip(
                rows, leg_estimates, pace_estimates, strict=True
            )
        ]
    )
    await session.commit()
    return await _execution_out(session, execution)


async def get_active_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> RouteExecutionOut | None:
    # "Active" here means "the run currently in progress" — a paused run is
    # still that run, just not making progress right now. Scoping this to
    # status == 'active' only would make a paused run invisible to a client
    # that reopens the app: it would see nothing here and start_execution
    # would happily create a second one, orphaning the paused run.
    execution = await session.scalar(
        select(RouteExecution).where(
            RouteExecution.user_id == user_id,
            RouteExecution.status.in_(("active", "paused")),
        )
    )
    if execution is not None and await _close_if_idle(session, execution, now=datetime.now(UTC)):
        return None
    return None if execution is None else await _execution_out(session, execution)


async def get_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
) -> RouteExecutionOut:
    execution = await _owned_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
    )
    return await _execution_out(session, execution)


async def list_executions(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int,
    offset: int,
) -> RouteExecutionListOut:
    where = RouteExecution.user_id == user_id
    total = int(
        await session.scalar(select(func.count()).select_from(RouteExecution).where(where)) or 0
    )
    executions = list(
        (
            await session.scalars(
                select(RouteExecution)
                .where(where)
                .order_by(RouteExecution.started_at.desc(), RouteExecution.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return RouteExecutionListOut(
        items=[await _execution_out(session, execution) for execution in executions],
        total=total,
        limit=limit,
        offset=offset,
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
    resolved = resolve_event_time(
        event.occurred_at if event is not None else None,
        now=now,
        not_before=max(execution.started_at, last_stop_at or execution.started_at),
    )
    execution.status = "completed"
    execution.completed_at = resolved.effective
    execution.updated_at = now
    await _award_completion_points(
        session,
        execution=execution,
        user=user,
        settings=await load_settings(session),
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


async def _award_completion_points(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    user: User,
    settings: AntiFraudSettings,
    now: datetime,
    finished_positions: set[int] | None = None,
    finished_days: int | None = None,
) -> None:
    """Grant travel points once, sized by what the route actually demanded.

    ``finished_positions`` limits the reward to the stops and legs of the
    days walked in full, for a run ended before its last day (spec 14, D21).

    Reads the immutable snapshot captured at start, so editing the route
    afterwards cannot change an already-earned reward. Cooldown, the daily cap
    and a flag hold are applied by ``antifraud_service.settle_completion_points``.
    ``points_status`` is the replay guard: a settled run is never paid twice.
    """

    if execution.points_status != "none" or execution.awarded_points:
        return

    required_done = (
        select(func.count())
        .select_from(RouteExecutionStop)
        .where(
            RouteExecutionStop.execution_id == execution.id,
            RouteExecutionStop.is_optional.is_(False),
            RouteExecutionStop.completed_at.is_not(None),
        )
    )
    if settings.enforcing:
        # A mark that followed the previous one too closely earns no stop points.
        required_done = required_done.where(RouteExecutionStop.mark_below_floor.is_(False))
    if finished_positions is not None:
        required_done = required_done.where(
            RouteExecutionStop.position.in_(finished_positions or {-1})
        )
    completed_required = int(await session.scalar(required_done) or 0)
    snapshot = (
        await session.get(RouteRoutingSnapshot, execution.routing_snapshot_id)
        if execution.routing_snapshot_id is not None
        else None
    )
    difficulty = snapshot.difficulty if snapshot is not None else None
    if difficulty is None and execution.route_id is not None:
        # Snapshots taken before spec 14 did not keep the difficulty.
        difficulty = await session.scalar(
            select(Route.difficulty).where(Route.id == execution.route_id)
        )
    segments: tuple[SegmentEffort, ...] = ()
    day_count = 1
    if snapshot is not None:
        segments = tuple(
            SegmentEffort(
                mode=row.mode,
                role=row.role,
                distance_meters=row.distance_meters,
                elevation_gain_meters=row.elevation_gain_meters,
            )
            for row in await session.scalars(
                select(RoutingSnapshotSegment)
                .where(RoutingSnapshotSegment.snapshot_id == snapshot.id)
                .order_by(RoutingSnapshotSegment.leg_index, RoutingSnapshotSegment.seq)
            )
            # Leg i leads to the stop at position i + 2.
            if finished_positions is None or row.leg_index + 2 in finished_positions
        )
        day_count = int(
            await session.scalar(
                select(func.count())
                .select_from(RoutingSnapshotDay)
                .where(RoutingSnapshotDay.snapshot_id == snapshot.id)
            )
            or 1
        )
        # Days set by hand never raise the cap above what the norms give (D20).
        if snapshot.auto_day_count:
            day_count = min(day_count, snapshot.auto_day_count)
    if finished_days is not None:
        day_count = min(day_count, max(1, finished_days))
    if finished_positions is not None and not finished_positions:
        # Not one day walked in full: nothing to pay for.
        await antifraud_service.settle_completion_points(
            session, execution=execution, user=user, points=0, settings=settings, now=now
        )
        return

    points = travel_points_for_effort(
        RouteEffort(
            completed_required_stops=completed_required,
            distance_meters=(
                snapshot.distance_meters if snapshot and finished_positions is None else None
            ),
            elevation_gain_meters=snapshot.elevation_gain_meters if snapshot else None,
            max_road_angle_degrees=snapshot.max_road_angle_degrees if snapshot else None,
            transport_mode=snapshot.transport_mode if snapshot else None,
            difficulty=difficulty,
            difficulty_level=snapshot.difficulty_reward if snapshot else None,
            segments=segments,
            day_count=day_count,
        )
    )
    await antifraud_service.settle_completion_points(
        session,
        execution=execution,
        user=user,
        points=points,
        settings=settings,
        now=now,
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


async def _finished_days(session: AsyncSession, execution: RouteExecution) -> tuple[set[int], int]:
    """Stop positions of the days whose required stops are all marked."""
    days = await _snapshot_days(session, execution)
    stops = list(
        await session.scalars(
            select(RouteExecutionStop).where(RouteExecutionStop.execution_id == execution.id)
        )
    )
    if not days and stops:
        days = [(1, max(stop.position for stop in stops))]
    positions: set[int] = set()
    count = 0
    for first, last in days:
        in_day = [stop for stop in stops if first <= stop.position <= last]
        if in_day and all(stop.completed_at is not None or stop.is_optional for stop in in_day):
            positions.update(stop.position for stop in in_day)
            count += 1
    return positions, count


async def _end_early(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    user: User,
    moment: datetime,
    now: datetime,
) -> None:
    """Cancel a run before its last day and pay for the days walked in full."""
    if execution.status == "paused" and execution.paused_at is not None:
        execution.paused_duration_seconds += max(
            0, int((moment - execution.paused_at).total_seconds())
        )
    execution.status = "cancelled"
    execution.cancelled_at = moment
    execution.paused_at = None
    execution.night_paused = False
    execution.ended_early = True
    execution.updated_at = now
    positions, count = await _finished_days(session, execution)
    await _award_completion_points(
        session,
        execution=execution,
        user=user,
        settings=await load_settings(session),
        now=now,
        finished_positions=positions,
        finished_days=count,
    )


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
