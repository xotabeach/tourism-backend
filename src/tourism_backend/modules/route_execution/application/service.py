"""Route execution state machine and ownership rules."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from geoalchemy2 import Geometry
from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.infrastructure.models import Place, RoadEvent
from tourism_backend.modules.route_builder.application.route_quality import (
    RoadEventSignal,
    active_road_event_blockers,
)
from tourism_backend.modules.route_builder.application.routing import normalize_transport_mode
from tourism_backend.modules.route_execution.application.routing_snapshot import (
    ensure_routing_snapshot,
    routing_snapshot_out,
)
from tourism_backend.modules.route_execution.application.schemas import (
    RouteExecutionListOut,
    RouteExecutionOut,
    RouteExecutionStatus,
    RouteExecutionStopOut,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionStop,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

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


async def _execution_out(session: AsyncSession, execution: RouteExecution) -> RouteExecutionOut:
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
    return RouteExecutionOut(
        id=execution.id,
        route_id=execution.route_id,
        route_name=execution.route_name,
        route_cover_url=execution.route_cover_url,
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
            )
            for stop in stops
        ],
        created_at=execution.created_at,
        updated_at=execution.updated_at,
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
            RouteExecution.status == "active",
        )
    )
    if active is not None:
        if active.route_id == route_id:
            return await _execution_out(session, active)
        raise AppError(
            code="active_route_execution_exists",
            message="Finish or cancel the active route first",
            status_code=409,
            details={"execution_id": str(active.id)},
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

    routing_snapshot = await ensure_routing_snapshot(
        session,
        route=route,
        stop_signature=[
            (route_stop.id, route_stop.position, place.id) for route_stop, place, _lng, _lat in rows
        ],
        captured_at=datetime.now(UTC),
    )

    cover_url = await session.scalar(
        select(MediaAttachment.public_path)
        .where(
            MediaAttachment.entity_type == "route",
            MediaAttachment.entity_id == route.id,
            MediaAttachment.role == "cover",
            MediaAttachment.status == "active",
        )
        .limit(1)
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
                created_at=now,
                updated_at=now,
            )
            for route_stop, place, lng, lat in rows
        ]
    )
    await session.commit()
    return await _execution_out(session, execution)


async def get_active_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> RouteExecutionOut | None:
    execution = await session.scalar(
        select(RouteExecution).where(
            RouteExecution.user_id == user_id,
            RouteExecution.status == "active",
        )
    )
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
) -> RouteExecutionOut:
    execution = await _owned_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
        for_update=True,
    )
    if execution.status != "active":
        raise AppError(
            code="route_execution_not_active",
            message="Route execution is not active",
            status_code=409,
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
        now = datetime.now(UTC)
        stop.completed_at = now
        stop.updated_at = now
        execution.updated_at = now
        await session.commit()
    return await _execution_out(session, execution)


async def complete_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
) -> RouteExecutionOut:
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
            details={"stop_ids": [str(stop.id) for stop in incomplete]},
        )
    now = datetime.now(UTC)
    execution.status = "completed"
    execution.completed_at = now
    execution.updated_at = now
    await session.commit()
    return await _execution_out(session, execution)


async def cancel_execution(
    session: AsyncSession,
    *,
    user_id: UUID,
    execution_id: UUID,
) -> RouteExecutionOut:
    execution = await _owned_execution(
        session,
        user_id=user_id,
        execution_id=execution_id,
        for_update=True,
    )
    if execution.status == "cancelled":
        return await _execution_out(session, execution)
    if execution.status != "active":
        raise AppError(
            code="route_execution_not_active",
            message="Route execution is not active",
            status_code=409,
        )
    now = datetime.now(UTC)
    execution.status = "cancelled"
    execution.cancelled_at = now
    execution.updated_at = now
    await session.commit()
    return await _execution_out(session, execution)
