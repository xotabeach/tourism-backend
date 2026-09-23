"""Keep a route's days and segments in step with its stops and routing.

Step 0 of spec 14: the rows are derived from what the route already has
(``accessibility.routing.legs`` and the stop order), so every write path that
changes stops or routing stays as it is. The run start refreshes them before
the snapshot is taken, and ``scripts/backfill_route_structure.py`` fills them
for existing routes.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.routes.application.structure_rules import (
    PlannedDay,
    PlannedSegment,
    base_mode_for,
    implies_public_transport,
    plan_segments,
    plan_single_day,
)
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteDay,
    RouteSegment,
    RouteStop,
)


def _routing(route: Route) -> dict[str, Any]:
    value = route.accessibility
    routing = value.get("routing") if isinstance(value, dict) else None
    return routing if isinstance(routing, dict) else {}


def _as_planned_segment(row: RouteSegment) -> PlannedSegment:
    return PlannedSegment(
        leg_index=row.leg_index,
        seq=row.seq,
        from_stop_id=row.from_stop_id,
        to_stop_id=row.to_stop_id,
        mode=row.mode,  # type: ignore[arg-type]
        role=row.role,  # type: ignore[arg-type]
        origin=row.origin,  # type: ignore[arg-type]
        distance_meters=row.distance_meters,
        duration_seconds=row.duration_seconds,
        elevation_gain_meters=row.elevation_gain_meters,
        elevation_loss_meters=row.elevation_loss_meters,
    )


def _as_planned_day(row: RouteDay) -> PlannedDay:
    return PlannedDay(
        day_index=row.day_index,
        first_stop_id=row.first_stop_id,
        last_stop_id=row.last_stop_id,
        boundary_source=row.boundary_source,  # type: ignore[arg-type]
    )


async def refresh_route_structure(
    session: AsyncSession,
    route: Route,
) -> tuple[list[PlannedSegment], list[PlannedDay]]:
    """Bring the route's segments and days up to date; return them in order.

    Rows are rewritten only when something changed, so a run start on an
    untouched route writes nothing. A leg an editor set by hand is kept as
    it is (spec 14, D3); so are days once someone moved a boundary (D8).
    """

    route.base_mode = base_mode_for(route.transport_mode)
    if implies_public_transport(route.transport_mode):
        route.needs_public_transport = True

    stop_ids = list(
        (
            await session.scalars(
                select(RouteStop.id)
                .where(RouteStop.route_id == route.id)
                .order_by(RouteStop.position)
            )
        ).all()
    )
    existing_segments = list(
        (
            await session.scalars(
                select(RouteSegment)
                .where(RouteSegment.route_id == route.id)
                .order_by(RouteSegment.leg_index, RouteSegment.seq)
            )
        ).all()
    )
    existing_days = list(
        (
            await session.scalars(
                select(RouteDay).where(RouteDay.route_id == route.id).order_by(RouteDay.day_index)
            )
        ).all()
    )

    planned = plan_segments(stop_ids, routing=_routing(route), base_mode=route.base_mode)
    edited: dict[tuple[UUID, UUID], list[RouteSegment]] = {}
    for row in existing_segments:
        if row.origin == "editor":
            edited.setdefault((row.from_stop_id, row.to_stop_id), [])
    for row in existing_segments:
        pair = (row.from_stop_id, row.to_stop_id)
        if pair in edited:
            edited[pair].append(row)
    segments: list[PlannedSegment] = []
    for segment in planned:
        kept = edited.get((segment.from_stop_id, segment.to_stop_id))
        if kept is None:
            segments.append(segment)
        else:
            segments.extend(
                replace(_as_planned_segment(row), leg_index=segment.leg_index) for row in kept
            )

    if [_as_planned_segment(row) for row in existing_segments] != segments:
        await session.execute(delete(RouteSegment).where(RouteSegment.route_id == route.id))
        for segment in segments:
            session.add(
                RouteSegment(
                    id=uuid4(),
                    route_id=route.id,
                    leg_index=segment.leg_index,
                    seq=segment.seq,
                    from_stop_id=segment.from_stop_id,
                    to_stop_id=segment.to_stop_id,
                    mode=segment.mode,
                    role=segment.role,
                    origin=segment.origin,
                    distance_meters=segment.distance_meters,
                    duration_seconds=segment.duration_seconds,
                    elevation_gain_meters=segment.elevation_gain_meters,
                    elevation_loss_meters=segment.elevation_loss_meters,
                )
            )

    stop_set = set(stop_ids)
    manual_intact = (
        route.days_manual
        and existing_days
        and all(d.first_stop_id in stop_set and d.last_stop_id in stop_set for d in existing_days)
    )
    days = (
        [_as_planned_day(row) for row in existing_days]
        if manual_intact
        else plan_single_day(stop_ids)
    )
    if [_as_planned_day(row) for row in existing_days] != days:
        await session.execute(delete(RouteDay).where(RouteDay.route_id == route.id))
        for day in days:
            session.add(
                RouteDay(
                    id=uuid4(),
                    route_id=route.id,
                    day_index=day.day_index,
                    first_stop_id=day.first_stop_id,
                    last_stop_id=day.last_stop_id,
                    boundary_source=day.boundary_source,
                )
            )
    await session.flush()
    return segments, days
