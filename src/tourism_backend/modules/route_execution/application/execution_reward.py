"""What a run earns: finished days, travel points and an early end."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.route_execution.application import antifraud_service
from tourism_backend.modules.route_execution.application.antifraud_settings import (
    AntiFraudSettings,
    load_settings,
)
from tourism_backend.modules.route_execution.application.execution_common import (
    _snapshot_days,
)
from tourism_backend.modules.route_execution.application.rewards import (
    RouteEffort,
    SegmentEffort,
    travel_points_for_effort,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionStop,
    RouteRoutingSnapshot,
    RoutingSnapshotDay,
    RoutingSnapshotSegment,
)
from tourism_backend.modules.routes.infrastructure.models import Route


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
