"""Anti-fraud orchestration for route runs (database side).

The pure rules live in ``antifraud_logic``; this module applies them to a run
inside the caller's transaction. Callers hold the user-row lock (see
``lock_user``), which serializes violation counting, flags, blocks and point
settlement per user.

Modes (``antifraud_settings.AntiFraudMode``):

* ``off`` - nothing is recorded or enforced.
* ``shadow`` - violations are recorded, nobody is flagged, blocked or held and
  points are credited exactly as before.
* ``enforce`` - limits, flags, blocks and holds apply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import get_settings
from tourism_backend.modules.identity.application.travel_points import (
    adjust_travel_points,
    award_travel_points,
)
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.notifications.application import service as notifications
from tourism_backend.modules.route_execution.application.antifraud_logic import (
    MOSCOW_TZ,
    GpsReading,
    GpsVerdict,
    PaceEvaluation,
    PaceKind,
    StopPoint,
    actual_leg_seconds,
    apply_daily_cap,
    cooldown_active,
    evaluate_escalation,
    evaluate_pace,
    gps_verdict,
    is_batched,
    moscow_day_bounds,
    next_block_step,
    paused_overlap_seconds,
)
from tourism_backend.modules.route_execution.application.antifraud_settings import (
    AntiFraudSettings,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionEvent,
    RouteExecutionStop,
    RoutePaceViolation,
    RoutePointsHold,
    UserFraudState,
)

_logger = logging.getLogger("tourism_backend.antifraud")

PaceVerdict = Literal["ok", "too_fast", "ahead", "unknown", "skipped"]

#: A mark that reaches us this long after it happened is treated as an offline sync.
OFFLINE_LAG = timedelta(minutes=2)
#: Only look this far back when counting violations for a flag or block.
_VIOLATION_LOOKBACK = timedelta(days=30)

MSK_LABEL = "МСК"


@dataclass(frozen=True, slots=True)
class PendingPush:
    user_id: UUID
    kind: notifications.AntiFraudKind
    title: str
    body: str


@dataclass(slots=True)
class MarkAssessment:
    """What the server concluded about one stop mark."""

    pace_verdict: PaceVerdict = "skipped"
    pushes: list[PendingPush] = field(default_factory=list)


async def lock_user(session: AsyncSession, user_id: UUID) -> User:
    """Serialize everything that touches one user's anti-fraud state."""

    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    return user


async def get_state(session: AsyncSession, user_id: UUID) -> UserFraudState | None:
    state: UserFraudState | None = await session.get(UserFraudState, user_id)
    return state


async def get_or_create_state(
    session: AsyncSession,
    user_id: UUID,
    *,
    now: datetime,
) -> UserFraudState:
    state = await get_state(session, user_id)
    if state is None:
        state = UserFraudState(
            user_id=user_id,
            is_flagged=False,
            ladder_level=0,
            is_trusted=False,
            updated_at=now,
        )
        session.add(state)
        await session.flush()
    return state


def msk_label(moment: datetime) -> str:
    return moment.astimezone(MOSCOW_TZ).strftime("%d.%m %H:%M") + f" {MSK_LABEL}"


# ------------------------------------------------------------ start guard


async def assert_start_allowed(
    session: AsyncSession,
    *,
    user_id: UUID,
    settings: AntiFraudSettings,
    now: datetime,
) -> None:
    """Refuse a new run while the user is blocked (only when enforcing)."""

    if not settings.enforcing:
        return
    state = await get_state(session, user_id)
    if state is None or state.blocked_until is None or state.blocked_until <= now:
        return
    raise AppError(
        code="route_start_blocked",
        message="Запуск маршрутов временно недоступен",
        status_code=403,
        details={
            "blocked_until": state.blocked_until.astimezone(UTC).isoformat(),
            "retryable": False,
        },
    )


# ------------------------------------------------------------ mark assessment


async def _pause_intervals(
    session: AsyncSession,
    execution_id: UUID,
) -> list[tuple[datetime, datetime | None]]:
    rows = (
        await session.execute(
            select(RouteExecutionEvent.action, RouteExecutionEvent.effective_at)
            .where(
                RouteExecutionEvent.execution_id == execution_id,
                RouteExecutionEvent.action.in_(("pause", "resume")),
                RouteExecutionEvent.applied.is_(True),
            )
            .order_by(RouteExecutionEvent.effective_at, RouteExecutionEvent.recorded_at)
        )
    ).all()
    intervals: list[tuple[datetime, datetime | None]] = []
    open_since: datetime | None = None
    for action, effective_at in rows:
        if action == "pause" and open_since is None:
            open_since = effective_at
        elif action == "resume" and open_since is not None:
            intervals.append((open_since, effective_at))
            open_since = None
    if open_since is not None:
        intervals.append((open_since, None))
    return intervals


def pace_estimate(stop: RouteExecutionStop) -> int | None:
    """The leg estimate the pace check judges by; older runs only have the shown one."""
    if stop.pace_estimate_seconds is not None:
        return stop.pace_estimate_seconds
    return stop.leg_estimate_seconds


def _observe_router_leg(
    stop: RouteExecutionStop,
    *,
    execution: RouteExecution,
    actual: int | None,
    judged: PaceEvaluation,
    settings: AntiFraudSettings,
) -> None:
    """Log when the router's own leg would judge this mark differently (spec 12a, D4).

    Observation only: nothing here is recorded as a violation or changes the
    mark. The log tells whether switching af_pace_source to the router's legs
    would flag more or fewer walkers.
    """
    router = stop.leg_estimate_seconds
    if (
        stop.leg_estimate_source != "provider"
        or router is None
        or stop.pace_estimate_seconds is None
        or router == stop.pace_estimate_seconds
    ):
        return
    shadow = evaluate_pace(estimate_seconds=router, actual_seconds=actual, rules=settings.pace)
    if shadow.kind is judged.kind and shadow.below_floor == judged.below_floor:
        return
    _logger.info(
        "pace_router_leg_disagrees",
        extra={
            "execution_id": str(execution.id),
            "stop_position": stop.position,
            "actual_seconds": actual,
            "straight_estimate_seconds": stop.pace_estimate_seconds,
            "router_estimate_seconds": router,
            "judged": judged.kind.value,
            "router_would_judge": shadow.kind.value,
            "judged_below_floor": judged.below_floor,
            "router_below_floor": shadow.below_floor,
        },
    )


async def assess_stop_mark(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    stop: RouteExecutionStop,
    effective_at: datetime,
    reported_at: datetime | None,
    position: GpsReading | None,
    settings: AntiFraudSettings,
    now: datetime,
) -> MarkAssessment:
    """Judge a stop mark, record any violation and escalate.

    ``stop.completed_at`` must already be set by the caller so the stop list
    reflects the mark being judged. Nothing is committed here.
    """

    assessment = MarkAssessment()
    if not settings.recording:
        return assessment

    state = await get_state(session, execution.user_id)
    if state is not None and state.is_trusted:
        return assessment

    stops = list(
        (
            await session.scalars(
                select(RouteExecutionStop)
                .where(RouteExecutionStop.execution_id == execution.id)
                .order_by(RouteExecutionStop.position)
            )
        ).all()
    )
    previous_mark_at = max(
        (
            other.completed_at
            for other in stops
            if other.id != stop.id
            and other.completed_at is not None
            and other.completed_at <= effective_at
        ),
        default=None,
    )
    paused = 0
    if previous_mark_at is not None:
        paused = paused_overlap_seconds(
            await _pause_intervals(session, execution.id),
            previous_mark_at,
            effective_at,
        )
    actual = actual_leg_seconds(
        previous_mark_at=previous_mark_at,
        this_mark_at=effective_at,
        paused_seconds=paused,
    )
    judged_by = pace_estimate(stop)
    pace = evaluate_pace(
        estimate_seconds=judged_by,
        actual_seconds=actual,
        rules=settings.pace,
    )
    _observe_router_leg(stop, execution=execution, actual=actual, judged=pace, settings=settings)
    gps = gps_verdict(
        position,
        stops=[StopPoint(item.position, item.lat, item.lng) for item in stops],
        marked_position=stop.position,
        rules=settings.gps,
    )

    # A mark that came too soon after the previous one earns no stop points.
    # The fact is recorded in shadow too; it only takes effect when enforcing.
    stop.mark_below_floor = pace.below_floor

    on_site = gps.verdict in (GpsVerdict.AT, GpsVerdict.BEHIND)
    violation_kind: Literal["too_fast", "ahead"] | None = None
    if gps.verdict is GpsVerdict.AHEAD:
        violation_kind = "ahead"
        assessment.pace_verdict = "ahead"
    elif on_site:
        # GPS places the user at or beyond this stop: the pace rule does not apply.
        assessment.pace_verdict = "ok"
    elif pace.kind is PaceKind.TOO_FAST:
        violation_kind = "too_fast"
        assessment.pace_verdict = "too_fast"
    elif pace.kind is PaceKind.OK:
        assessment.pace_verdict = "ok"
    elif position is not None:
        assessment.pace_verdict = "unknown"

    if violation_kind is None:
        return assessment

    previous_counted = await session.scalar(
        select(func.max(RoutePaceViolation.occurred_at)).where(
            RoutePaceViolation.execution_id == execution.id,
            RoutePaceViolation.counted.is_(True),
        )
    )
    counted = not is_batched(
        previous_counted,
        effective_at,
        window_seconds=settings.batch_window_seconds,
    )
    session.add(
        RoutePaceViolation(
            id=uuid4(),
            user_id=execution.user_id,
            execution_id=execution.id,
            stop_id=stop.id,
            kind=violation_kind,
            estimate_seconds=judged_by,
            actual_seconds=actual,
            gps_verdict=gps.verdict.value if position is not None else None,
            gps_distance_bucket_m=gps.distance_bucket_m,
            timing_source="device" if reported_at is not None else "server",
            offline_sync=reported_at is not None and (now - reported_at) > OFFLINE_LAG,
            counted=counted,
            mode=settings.mode.value,
            occurred_at=effective_at,
            created_at=now,
        )
    )
    await session.flush()

    if counted and settings.enforcing:
        await _escalate(session, execution=execution, settings=settings, now=now, into=assessment)
    return assessment


# ------------------------------------------------------------ escalation


async def _escalate(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    settings: AntiFraudSettings,
    now: datetime,
    into: MarkAssessment,
) -> None:
    times = list(
        (
            await session.scalars(
                select(RoutePaceViolation.occurred_at)
                .where(
                    RoutePaceViolation.user_id == execution.user_id,
                    RoutePaceViolation.counted.is_(True),
                    RoutePaceViolation.occurred_at >= now - _VIOLATION_LOOKBACK,
                )
                .order_by(RoutePaceViolation.occurred_at)
            )
        ).all()
    )
    if not times:
        return
    state = await get_or_create_state(session, execution.user_id, now=now)
    # Windows end at the newest counted violation, so a late offline sync of
    # old marks cannot hide a burst by arriving after the window has passed.
    decision = evaluate_escalation(
        times,
        now=max(times),
        counters_from=state.counters_from,
        rules=settings.escalation,
    )
    if decision.should_block:
        step = next_block_step(
            ladder_level=state.ladder_level,
            last_offence_at=state.last_offence_at,
            now=now,
            rules=settings.escalation,
        )
        new_until = now + timedelta(hours=step.duration_hours)
        if state.blocked_until is None or state.blocked_until < new_until:
            state.blocked_until = new_until
        state.ladder_level = step.new_ladder_level
        state.last_offence_at = now
        state.counters_from = now
        state.updated_at = now
        if not state.is_flagged:
            await _flag(session, state=state, settings=settings, now=now, notify=False, into=into)
        push = PendingPush(
            user_id=execution.user_id,
            kind="antifraud_blocked",
            title="Запуск маршрутов временно недоступен",
            body=(
                "Мы заметили необычно быстрое прохождение. Новые маршруты можно "
                f"запускать после {msk_label(new_until)}. Очки за последние "
                "прохождения проверит команда."
            ),
        )
        into.pushes.append(push)
        await notifications.create_antifraud_notification(
            session,
            user_id=execution.user_id,
            kind=push.kind,
            title=push.title,
            body=push.body,
        )
    elif decision.should_flag and not state.is_flagged:
        await _flag(session, state=state, settings=settings, now=now, notify=True, into=into)


async def _flag(
    session: AsyncSession,
    *,
    state: UserFraudState,
    settings: AntiFraudSettings,
    now: datetime,
    notify: bool,
    into: MarkAssessment,
) -> None:
    """Flag the user and pull back points of runs that contain counted violations."""

    state.is_flagged = True
    state.flagged_at = now
    state.updated_at = now
    await _hold_recent_runs(session, state=state, settings=settings, now=now)
    if notify:
        push = PendingPush(
            user_id=state.user_id,
            kind="antifraud_flagged",
            title="Очки за маршруты проверяются",
            body=(
                "Мы заметили необычно быстрое прохождение маршрутов. Очки за "
                "последние прохождения проверит команда."
            ),
        )
        into.pushes.append(push)
        await notifications.create_antifraud_notification(
            session,
            user_id=state.user_id,
            kind=push.kind,
            title=push.title,
            body=push.body,
        )


async def _hold_recent_runs(
    session: AsyncSession,
    *,
    state: UserFraudState,
    settings: AntiFraudSettings,
    now: datetime,
) -> None:
    """Retro-hold: credited runs holding counted violations in the flag window."""

    lower = now - timedelta(hours=settings.escalation.flag_window_hours)
    if state.counters_from is not None:
        lower = max(lower, state.counters_from)
    execution_ids = list(
        (
            await session.scalars(
                select(RoutePaceViolation.execution_id)
                .where(
                    RoutePaceViolation.user_id == state.user_id,
                    RoutePaceViolation.counted.is_(True),
                    RoutePaceViolation.occurred_at >= lower,
                )
                .distinct()
            )
        ).all()
    )
    if not execution_ids:
        return
    executions = list(
        (
            await session.scalars(
                select(RouteExecution).where(
                    RouteExecution.id.in_(execution_ids),
                    RouteExecution.user_id == state.user_id,
                    RouteExecution.status == "completed",
                    RouteExecution.points_status == "awarded",
                    RouteExecution.awarded_points > 0,
                )
            )
        ).all()
    )
    if not executions:
        return
    user = await session.get(User, state.user_id)
    if user is None:
        return
    for execution in executions:
        amount = int(execution.awarded_points)
        taken = -await adjust_travel_points(
            session,
            user=user,
            delta=-min(amount, max(0, user.travel_points)),
        )
        session.add(
            RoutePointsHold(
                id=uuid4(),
                user_id=state.user_id,
                execution_id=execution.id,
                amount=amount,
                deducted_points=taken,
                status="held",
                reason="flag_retro",
                created_at=now,
            )
        )
        execution.points_status = "held"
        execution.awarded_points = 0
        execution.updated_at = now


# ------------------------------------------------------------ point settlement


async def _last_paid_completion(
    session: AsyncSession,
    *,
    execution: RouteExecution,
) -> datetime | None:
    if execution.route_id is None:
        return None
    moment: datetime | None = await session.scalar(
        select(func.max(RouteExecution.completed_at)).where(
            RouteExecution.user_id == execution.user_id,
            RouteExecution.route_id == execution.route_id,
            RouteExecution.id != execution.id,
            RouteExecution.status == "completed",
            RouteExecution.points_status.in_(("awarded", "held")),
            RouteExecution.computed_points > 0,
        )
    )
    return moment


async def _computed_today(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    moment: datetime,
) -> int:
    start, end = moscow_day_bounds(moment)
    total = await session.scalar(
        select(func.coalesce(func.sum(RouteExecution.computed_points), 0)).where(
            RouteExecution.user_id == execution.user_id,
            RouteExecution.id != execution.id,
            RouteExecution.status == "completed",
            RouteExecution.completed_at >= start,
            RouteExecution.completed_at < end,
        )
    )
    return int(total or 0)


async def settle_completion_points(
    session: AsyncSession,
    *,
    execution: RouteExecution,
    user: User,
    points: int,
    settings: AntiFraudSettings,
    now: datetime,
) -> MarkAssessment:
    """Apply cooldown, daily cap and a flag hold, then credit or park the points."""

    outcome = MarkAssessment()
    reason: str | None = None
    final = points

    if settings.recording and points > 0:
        completed_at = execution.completed_at or now
        last_paid = await _last_paid_completion(session, execution=execution)
        if cooldown_active(
            last_awarded_completed_at=last_paid,
            now=completed_at,
            cooldown_days=settings.route_points_cooldown_days,
        ):
            final, reason = 0, "route_cooldown"
        else:
            capped = apply_daily_cap(
                points=points,
                awarded_today=await _computed_today(
                    session,
                    execution=execution,
                    moment=completed_at,
                ),
                cap=settings.daily_points_cap,
            )
            if capped < points:
                final, reason = capped, "daily_cap"

    if reason is not None and not settings.enforcing:
        # Shadow: report what would have happened, change nothing.
        _logger.info(
            "antifraud_shadow_limit",
            extra={"reason": reason, "points": points, "would_award": final},
        )
        final, reason = points, None

    execution.computed_points = final
    execution.points_reason = reason

    state = await get_state(session, execution.user_id) if settings.enforcing else None
    if final > 0 and state is not None and state.is_flagged and not state.is_trusted:
        session.add(
            RoutePointsHold(
                id=uuid4(),
                user_id=execution.user_id,
                execution_id=execution.id,
                amount=final,
                deducted_points=0,
                status="held",
                reason="flag_forward",
                created_at=now,
            )
        )
        execution.awarded_points = 0
        execution.points_status = "held"
        return outcome

    credited = await award_travel_points(session, user=user, points=final)
    execution.awarded_points = credited
    execution.points_status = "awarded" if credited > 0 else "none"
    return outcome


async def deliver_pushes(session: AsyncSession, pushes: list[PendingPush]) -> None:
    """Best-effort FCM for notifications already stored; call after the commit.

    The inbox row is the source of truth: a failed push is logged by the FCM
    sender and never fails the request. ``target_type="inbox"`` is a push-only
    routing hint that every client version sends to the inbox.
    """

    for push in pushes:
        await notifications.maybe_push_notification(
            session,
            get_settings(),
            user_id=push.user_id,
            kind=push.kind,
            title=push.title,
            body=push.body,
            target_type="inbox",
            target_id=push.user_id,
        )
