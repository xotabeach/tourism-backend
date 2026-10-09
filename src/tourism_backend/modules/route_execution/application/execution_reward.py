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
    StopState,
    completed_share_percent,
    paid_leg_positions,
    paid_way_share,
    run_counts,
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
    ended_early: bool = False,
) -> None:
    """Grant travel points once, sized by what the walker really did.

    Only the legs that were walked are paid (spec 15, D1, D14, D15): the way
    to a marked stop, and to one skipped as «closed». A stop skipped for
    another reason, or not reached before an early end, takes its leg out.
    The same rule holds for one-day and multi-day runs.

    Reads the immutable snapshot captured at start, so editing the route
    afterwards cannot change an already-earned reward. Cooldown, the daily cap
    and a flag hold are applied by ``antifraud_service.settle_completion_points``.
    ``points_status`` is the replay guard: a settled run is never paid twice.
    """

    if execution.points_status != "none" or execution.awarded_points:
        return

    stops = list(
        await session.scalars(
            select(RouteExecutionStop)
            .where(RouteExecutionStop.execution_id == execution.id)
            .order_by(RouteExecutionStop.position)
        )
    )
    if not any(stop.completed_at is not None for stop in stops):
        # Nothing was reached: nothing to pay for.
        await antifraud_service.settle_completion_points(
            session, execution=execution, user=user, points=0, settings=settings, now=now
        )
        return

    completed_required = sum(
        1
        for stop in stops
        if not stop.is_optional
        and stop.completed_at is not None
        # A mark that followed the previous one too closely earns no stop points.
        and not (settings.enforcing and stop.mark_below_floor)
    )
    paid = paid_leg_positions(
        [
            StopState(
                position=stop.position,
                is_optional=stop.is_optional,
                marked=stop.completed_at is not None,
                skip_reason=stop.skip_reason,
            )
            for stop in stops
        ],
        run_completed=not ended_early,
    )
    # Leg i leads to the stop at position i + 2; the first stop has no leg.
    leg_lengths = [
        (stop.leg_distance_meters, stop.position in paid) for stop in stops if stop.position >= 2
    ]
    partial = any(not is_paid for _meters, is_paid in leg_lengths)

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
    paid_share = 1.0
    if snapshot is not None:
        rows = list(
            await session.scalars(
                select(RoutingSnapshotSegment)
                .where(RoutingSnapshotSegment.snapshot_id == snapshot.id)
                .order_by(RoutingSnapshotSegment.leg_index, RoutingSnapshotSegment.seq)
            )
        )
        judged = [
            (
                SegmentEffort(
                    mode=row.mode,
                    role=row.role,
                    distance_meters=row.distance_meters,
                    elevation_gain_meters=row.elevation_gain_meters,
                ),
                not partial or row.leg_index + 2 in paid,
            )
            for row in rows
        ]
        segments = tuple(segment for segment, is_paid in judged if is_paid)
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
        if partial:
            paid_share = paid_way_share(judged, leg_lengths)
            # The cap grows with the days actually walked on, not planned.
            days = await _snapshot_days(session, execution)
            walked_days = sum(
                any(first <= position <= last for position in paid) for first, last in days
            )
            day_count = min(day_count, max(1, walked_days))
    elif partial:
        paid_share = paid_way_share([], leg_lengths)

    if snapshot is not None:
        execution.paid_distance_meters = _share_of(snapshot.distance_meters, paid_share)

    points = travel_points_for_effort(
        RouteEffort(
            completed_required_stops=completed_required,
            # Totals of the route stand whole for a route walked whole and
            # in the walked share otherwise. They only matter for a snapshot
            # whose segments carry no numbers of their own.
            distance_meters=_share_of(snapshot.distance_meters if snapshot else None, paid_share),
            elevation_gain_meters=_share_of(
                snapshot.elevation_gain_meters if snapshot else None, paid_share
            ),
            max_road_angle_degrees=snapshot.max_road_angle_degrees if snapshot else None,
            transport_mode=snapshot.transport_mode if snapshot else None,
            difficulty=difficulty,
            difficulty_level=snapshot.difficulty_reward if snapshot else None,
            segments=segments,
            day_count=day_count,
            paid_share=paid_share,
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


def _share_of(total: int | None, share: float) -> int | None:
    if total is None or share >= 1.0:
        return total
    return round(total * max(0.0, share))


async def _required_share(session: AsyncSession, execution: RouteExecution) -> int:
    required = list(
        await session.scalars(
            select(RouteExecutionStop).where(
                RouteExecutionStop.execution_id == execution.id,
                RouteExecutionStop.is_optional.is_(False),
            )
        )
    )
    return completed_share_percent(
        sum(stop.completed_at is not None for stop in required), len(required)
    )


async def _close_abandoned(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    user: User,
    moment: datetime,
    now: datetime,
) -> bool:
    """Close a run nobody came back to; True when it was closed as walked.

    Someone who walked the route and forgot «Завершить» must not lose it
    (spec 15, D16): when the share of marked stops reaches the counting
    threshold the run is closed as completed and counts. Otherwise it is
    cancelled as ended early. Either way the walked legs are paid, and the
    stops never reached are not.
    """
    settings = await load_settings(session)
    share = await _required_share(session, execution)
    if not run_counts(share, settings.counted_stops_percent):
        await _end_early(session, execution=execution, user=user, moment=moment, now=now)
        return False
    if execution.status == "paused" and execution.paused_at is not None:
        execution.paused_duration_seconds += max(
            0, int((moment - execution.paused_at).total_seconds())
        )
    execution.status = "completed"
    execution.completed_at = moment
    execution.paused_at = None
    execution.night_paused = False
    execution.counted = True
    execution.completed_share_percent = share
    execution.updated_at = now
    await _award_completion_points(
        session,
        execution=execution,
        user=user,
        settings=settings,
        now=now,
        ended_early=True,
    )
    return True


async def _end_early(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    user: User,
    moment: datetime,
    now: datetime,
) -> None:
    """Cancel a run before its end and pay for what was walked.

    A run its walker ended early does not count as «прошёл маршрут»,
    however much of it was walked: counting is for a completed run.
    """
    if execution.status == "paused" and execution.paused_at is not None:
        execution.paused_duration_seconds += max(
            0, int((moment - execution.paused_at).total_seconds())
        )
    execution.status = "cancelled"
    execution.cancelled_at = moment
    execution.paused_at = None
    execution.night_paused = False
    execution.ended_early = True
    execution.counted = False
    execution.completed_share_percent = await _required_share(session, execution)
    execution.updated_at = now
    await _award_completion_points(
        session,
        execution=execution,
        user=user,
        settings=await load_settings(session),
        now=now,
        ended_early=True,
    )
