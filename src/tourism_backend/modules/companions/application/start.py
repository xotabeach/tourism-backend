"""The group's start (BACKEND-60, spec 11b D4, D5, D11, D12).

The organizer gives the start; the server never starts a run for anyone.
Every ready phone then starts its own run with the group's start time and
names the group, which attaches the run here. Whoever could not start (no
network, an anti-fraud stop, a road closure) stays in the group as «не
стартовал» and comes in later on the same route, marked «присоединился
позже».
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.companions.application import groups
from tourism_backend.modules.companions.application.schemas import CompanionGroupOut
from tourism_backend.modules.companions.infrastructure.models import (
    CompanionGroup,
    CompanionMember,
)
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.route_execution.infrastructure.models import RouteExecution
from tourism_backend.modules.routes.infrastructure.models import Route

#: A start this long after the group's makes the member «присоединился
#: позже»: the phone that was simply slow to react is not late.
LATE_AFTER_SECONDS = 15 * 60


def _now() -> datetime:
    return datetime.now(UTC)


async def _unfinished_run(session: AsyncSession, user_id: UUID) -> RouteExecution | None:
    run: RouteExecution | None = await session.scalar(
        select(RouteExecution).where(
            RouteExecution.user_id == user_id,
            RouteExecution.status.in_(("active", "paused")),
        )
    )
    return run


async def set_ready(
    session: AsyncSession, *, group_id: str, user_id: UUID, ready: bool
) -> CompanionGroupOut:
    group, member = await groups._visible_group(session, group_id, user_id)
    assert member is not None
    if member.state != "joined":
        raise AppError(code="companion_not_member", message="Вы не в группе", status_code=403)
    groups._require_gathering(group)
    if ready:
        run = await _unfinished_run(session, user_id)
        if run is not None:
            # D12: the phone could not start the group's run anyway.
            raise AppError(
                code="companion_busy_run",
                message="Сначала завершите текущий маршрут",
                status_code=409,
                details={"execution_id": str(run.id), "route_name": run.route_name},
            )
    member.ready = ready
    member.updated_at = _now()
    await session.commit()
    return await groups._out(session, group, user_id)


async def start_group(
    session: AsyncSession, *, settings: Settings | None, group_id: str, actor_id: UUID
) -> CompanionGroupOut:
    group = await groups._group_for(session, group_id)
    groups._require_organizer(group, actor_id)
    await groups._check_route_still_open(session, settings, group)
    groups._require_gathering(group)
    members = [m for m in await groups._members(session, group.id) if m.state == "joined"]
    if len(members) < 2:
        raise AppError(
            code="companion_group_alone",
            message="Позовите хотя бы одного попутчика",
            status_code=409,
        )
    route = await session.get(Route, group.route_id)
    assert route is not None
    now = _now()
    group.status = "started"
    group.started_at = now
    # Everyone starts now, so everyone walks the route as it is now (D13).
    group.route_version_at = route.content_updated_at
    group.updated_at = now
    for member in members:
        if member.user_id == actor_id:
            member.ready = True
    organizer = await session.get(User, actor_id)
    pushes = await groups._notify(
        session,
        settings,
        user_ids=[m.user_id for m in members],
        kind="companion_group_started",
        title="Группа вышла на маршрут",
        body=(
            f"{organizer.display_name if organizer else 'Организатор'} дал старт: "
            f"«{route.name}». Откройте приложение, чтобы начать."
        ),
        group_id=group.id,
        actor_id=actor_id,
    )
    await session.commit()
    await groups._push(session, settings, pushes, group.id)
    return await groups._out(session, group, actor_id)


async def group_for_run(
    session: AsyncSession, *, group_id: UUID, user_id: UUID, route_id: UUID
) -> CompanionGroup:
    """The group a run is about to be started in, or why it cannot be."""
    group = await session.get(CompanionGroup, group_id)
    member = None if group is None else await groups._member(session, group.id, user_id)
    if group is None or member is None or member.state != "joined":
        raise groups._not_found()
    if group.status != "started" or group.started_at is None:
        raise AppError(
            code="companion_group_not_started",
            message="Организатор ещё не дал старт",
            status_code=409,
        )
    if group.route_id != route_id:
        raise AppError(
            code="companion_route_mismatch",
            message="У группы другой маршрут",
            status_code=409,
        )
    route = await session.get(Route, route_id)
    if route is None or route.content_updated_at != group.route_version_at:
        # A late walker would follow other stops than the group (D13).
        raise AppError(
            code="companion_route_changed",
            message="Маршрут обновился после старта группы, присоединиться уже нельзя",
            status_code=409,
        )
    return group


async def attach_run(
    session: AsyncSession, *, group: CompanionGroup, user_id: UUID, execution: RouteExecution
) -> None:
    """Links the member's own run to the group; the caller commits."""
    member = await groups._member(session, group.id, user_id)
    if member is None or member.execution_id == execution.id:
        return
    member.execution_id = execution.id
    assert group.started_at is not None
    started = execution.started_at or group.started_at
    member.joined_late = member.joined_late or (
        not member.ready or (started - group.started_at).total_seconds() > LATE_AFTER_SECONDS
    )
    member.updated_at = _now()


async def membership_for_run(session: AsyncSession, execution_id: UUID) -> CompanionMember | None:
    member: CompanionMember | None = await session.scalar(
        select(CompanionMember).where(CompanionMember.execution_id == execution_id)
    )
    return member
