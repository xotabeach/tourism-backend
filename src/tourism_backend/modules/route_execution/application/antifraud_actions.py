"""Operator actions on anti-fraud state (used by the admin panel).

Every action takes the user-row lock first, so it cannot interleave with a
stop mark or a completion of the same user, then works on the rows it owns.
Audit records are written by the admin layer (``admin_audit_events``), which
knows the acting principal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.achievements.service import after_commit as evaluate_achievements
from tourism_backend.modules.identity.application.travel_points import adjust_travel_points
from tourism_backend.modules.notifications.application import service as notifications
from tourism_backend.modules.route_execution.application import antifraud_service
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RoutePointsHold,
    UserFraudState,
)

MAX_MANUAL_BLOCK_HOURS = 24 * 90


async def decide_hold(
    session: AsyncSession,
    *,
    hold_id: UUID,
    approve: bool,
    principal_id: UUID | None,
    note: str | None = None,
    now: datetime | None = None,
) -> RoutePointsHold:
    """Approve (points return) or reject (points stay gone) one held run.

    Idempotent: deciding an already-decided hold changes nothing, so two
    operators clicking at once cannot pay or cancel the same points twice.
    """

    moment = now or datetime.now(UTC)
    found_user_id = await session.scalar(
        select(RoutePointsHold.user_id).where(RoutePointsHold.id == hold_id)
    )
    if found_user_id is None:
        raise AppError(code="points_hold_not_found", message="Hold not found", status_code=404)
    user = await antifraud_service.lock_user(session, found_user_id)
    hold = await session.scalar(
        select(RoutePointsHold).where(RoutePointsHold.id == hold_id).with_for_update()
    )
    if hold is None:
        raise AppError(code="points_hold_not_found", message="Hold not found", status_code=404)
    if hold.status != "held":
        return hold

    execution = await session.get(RouteExecution, hold.execution_id)
    credited = 0
    if approve:
        # A retro hold gives back exactly what it took; a forward hold never
        # touched the balance, so the whole amount is paid now.
        credit = hold.amount if hold.reason == "flag_forward" else hold.deducted_points
        credited = await adjust_travel_points(session, user=user, delta=credit)
    hold.status = "approved" if approve else "rejected"
    hold.decided_at = moment
    hold.decided_by = principal_id
    hold.note = (note or "").strip()[:500] or None
    route_name = "маршрут"
    if execution is not None:
        execution.points_status = "awarded" if approve else "rejected"
        execution.awarded_points = credited if approve else 0
        execution.updated_at = moment
        route_name = f"«{execution.route_name}»"

    push = antifraud_service.PendingPush(
        user_id=hold.user_id,
        kind="antifraud_points_decision",
        title="Очки за маршрут: проверка завершена",
        body=(
            f"Очки за {route_name} возвращены."
            if approve
            else f"Очки за {route_name} не начислены."
        ),
    )
    await notifications.create_antifraud_notification(
        session,
        user_id=push.user_id,
        kind=push.kind,
        title=push.title,
        body=push.body,
    )
    await session.commit()
    if approve:
        await evaluate_achievements(session, hold.user_id)
    await antifraud_service.deliver_pushes(session, [push])
    return hold


async def reset_flag(
    session: AsyncSession,
    *,
    user_id: UUID,
    now: datetime | None = None,
) -> UserFraudState:
    """Clear the flag and start counting from scratch (holds are decided separately)."""

    moment = now or datetime.now(UTC)
    await antifraud_service.lock_user(session, user_id)
    state = await antifraud_service.get_or_create_state(session, user_id, now=moment)
    state.is_flagged = False
    state.flagged_at = None
    state.ladder_level = 0
    state.last_offence_at = None
    state.counters_from = moment
    state.updated_at = moment
    await session.commit()
    await evaluate_achievements(session, user_id)
    return state


async def lift_block(
    session: AsyncSession,
    *,
    user_id: UUID,
    now: datetime | None = None,
) -> UserFraudState:
    moment = now or datetime.now(UTC)
    await antifraud_service.lock_user(session, user_id)
    state = await antifraud_service.get_or_create_state(session, user_id, now=moment)
    state.blocked_until = None
    state.updated_at = moment
    await session.commit()
    return state


async def block_manually(
    session: AsyncSession,
    *,
    user_id: UUID,
    hours: int,
    now: datetime | None = None,
) -> UserFraudState:
    """Block new runs for ``hours``; a longer existing block is kept."""

    if not 1 <= hours <= MAX_MANUAL_BLOCK_HOURS:
        raise AppError(
            code="invalid_block_duration",
            message=f"Срок блокировки: от 1 до {MAX_MANUAL_BLOCK_HOURS} часов",
            status_code=422,
        )
    moment = now or datetime.now(UTC)
    await antifraud_service.lock_user(session, user_id)
    state = await antifraud_service.get_or_create_state(session, user_id, now=moment)
    until = moment + timedelta(hours=hours)
    if state.blocked_until is None or state.blocked_until < until:
        state.blocked_until = until
    state.updated_at = moment
    await session.commit()
    return state


async def set_trusted(
    session: AsyncSession,
    *,
    user_id: UUID,
    trusted: bool,
    now: datetime | None = None,
) -> UserFraudState:
    """Exclude a user from pace detection (caps and cooldown still apply)."""

    moment = now or datetime.now(UTC)
    await antifraud_service.lock_user(session, user_id)
    state = await antifraud_service.get_or_create_state(session, user_id, now=moment)
    state.is_trusted = trusted
    state.updated_at = moment
    await session.commit()
    return state


async def count_overdue_holds(
    session: AsyncSession,
    *,
    older_than_days: int,
    now: datetime | None = None,
) -> int:
    """Holds still waiting after ``older_than_days`` - the review-queue alarm."""

    moment = now or datetime.now(UTC)
    total = await session.scalar(
        select(func.count())
        .select_from(RoutePointsHold)
        .where(
            RoutePointsHold.status == "held",
            RoutePointsHold.created_at < moment - timedelta(days=older_than_days),
        )
    )
    return int(total or 0)


__all__ = (
    "MAX_MANUAL_BLOCK_HOURS",
    "block_manually",
    "count_overdue_holds",
    "decide_hold",
    "lift_block",
    "reset_flag",
    "set_trusted",
)
