"""Starting a run and reading runs back."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import cast as sa_cast
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.places.infrastructure.models import Place, RoadEvent
from tourism_backend.modules.route_builder.application.route_quality import (
    RoadEventSignal,
    active_road_event_blockers,
)
from tourism_backend.modules.route_builder.application.routing import normalize_transport_mode
from tourism_backend.modules.route_execution.application import antifraud_service
from tourism_backend.modules.route_execution.application.antifraud_logic import (
    StopPoint,
    build_leg_estimates,
    provider_legs_from_metadata,
)
from tourism_backend.modules.route_execution.application.antifraud_settings import (
    load_settings,
)
from tourism_backend.modules.route_execution.application.execution_common import (
    _PUBLIC_ROUTE,
    _execution_out,
    _owned_execution,
    _owned_route,
)
from tourism_backend.modules.route_execution.application.execution_days import (
    _close_if_idle,
)
from tourism_backend.modules.route_execution.application.routing_snapshot import (
    ensure_routing_snapshot,
)
from tourism_backend.modules.route_execution.application.schemas import (
    RouteExecutionListOut,
    RouteExecutionOut,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionStop,
)
from tourism_backend.modules.routes.application.service import route_cover_urls
from tourism_backend.modules.routes.application.structure import refresh_route_structure
from tourism_backend.modules.routes.application.structure_rules import segment_mode_for
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop


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
