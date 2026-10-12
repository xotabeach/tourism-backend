"""Progress, result and «кто первым дошёл» of a group (BACKEND-61, spec 11b).

Everything is read from the members' own runs: their stop marks with time.
No positions, no points for the group (D6, D7, D14).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.config import Settings
from tourism_backend.modules.companions.application import groups
from tourism_backend.modules.companions.application.schemas import (
    CompanionMemberProgressOut,
    CompanionProgressOut,
    CompanionStopMarkOut,
    CompanionStopOut,
    CompanionSummaryOut,
)
from tourism_backend.modules.companions.infrastructure.models import (
    CompanionGroup,
    CompanionMember,
)
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.route_execution.infrastructure.models import (
    RouteExecution,
    RouteExecutionEvent,
    RouteExecutionStop,
)
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

#: The group's result is fixed this long after the start at the latest (D14).
SETTLE_AFTER = timedelta(days=7)
#: A member who never started holds the result back no longer than this
#: once everyone who did start has ended.
UNSTARTED_GRACE = timedelta(hours=24)
#: A mark that reached the server this late was made without network.
OFFLINE_AFTER = timedelta(minutes=2)
#: «Компания» (D16): shared stops marked within this time of each other.
COMPANY_WINDOW = timedelta(hours=1)
#: Until the «горный маршрут» flag (BACKEND-39) exists, a route this hard
#: counts as one, and the table is hidden on it.
MOUNTAIN_LEVEL = 4

_ENDED = ("completed", "cancelled")


def _now() -> datetime:
    return datetime.now(UTC)


def is_mountain(route: Route | None) -> bool:
    if route is None:
        return False
    flag = getattr(route, "is_mountain", None)
    if isinstance(flag, bool):
        return flag
    return (route.difficulty_level or 0) >= MOUNTAIN_LEVEL


async def _joined(session: AsyncSession, group_id: UUID) -> list[CompanionMember]:
    return [m for m in await groups._members(session, group_id) if m.state == "joined"]


async def _runs(
    session: AsyncSession, members: list[CompanionMember]
) -> dict[UUID, RouteExecution]:
    ids = [m.execution_id for m in members if m.execution_id is not None]
    if not ids:
        return {}
    rows = (await session.scalars(select(RouteExecution).where(RouteExecution.id.in_(ids)))).all()
    return {run.id: run for run in rows}


def _due(
    group: CompanionGroup,
    members: list[CompanionMember],
    runs: dict[UUID, RouteExecution],
    now: datetime,
) -> bool:
    if group.status != "started" or group.started_at is None:
        return False
    if now >= group.started_at + SETTLE_AFTER:
        return True
    started = [runs.get(m.execution_id) for m in members if m.execution_id is not None]
    ended: list[RouteExecution] = [run for run in started if run is not None]
    if not started or any(run is None or run.status not in _ENDED for run in started):
        return False
    if all(m.execution_id is not None for m in members):
        return True
    last_end: datetime = max(
        run.completed_at or run.cancelled_at or run.updated_at for run in ended
    )
    return now >= last_end + UNSTARTED_GRACE


async def settle_if_due(session: AsyncSession, group: CompanionGroup) -> bool:
    """Fixes the group's result when everyone has ended or time is up."""
    members = await _joined(session, group.id)
    runs = await _runs(session, members)
    now = _now()
    if not _due(group, members, runs, now):
        return False
    group.status = "settled"
    group.settled_at = now
    group.updated_at = now
    await session.commit()
    return True


async def _offline_stops(session: AsyncSession, run_ids: list[UUID]) -> set[UUID]:
    if not run_ids:
        return set()
    events = (
        await session.scalars(
            select(RouteExecutionEvent).where(
                RouteExecutionEvent.execution_id.in_(run_ids),
                RouteExecutionEvent.action == "complete_stop",
                RouteExecutionEvent.applied.is_(True),
                RouteExecutionEvent.occurred_at.is_not(None),
            )
        )
    ).all()
    return {
        event.stop_id
        for event in events
        if event.stop_id is not None
        and event.occurred_at is not None
        and event.recorded_at - event.occurred_at > OFFLINE_AFTER
    }


def _status(group: CompanionGroup, member: CompanionMember, run: RouteExecution | None) -> str:
    if run is None:
        return "not_finished" if group.status == "settled" else "not_started"
    if run.status == "completed":
        return "finished"
    if run.status == "cancelled":
        return "quit"
    if group.status == "settled":
        return "not_finished"
    return "paused" if run.status == "paused" else "walking"


async def group_progress(
    session: AsyncSession, *, settings: Settings | None, group_id: str, user_id: UUID
) -> CompanionProgressOut:
    group, member = await groups._visible_group(session, group_id, user_id)
    inside = group.organizer_user_id == user_id or (member is not None and member.state == "joined")
    if not inside:
        raise groups._not_found()
    await groups._check_route_still_open(session, settings, group)
    await settle_if_due(session, group)

    members = await _joined(session, group.id)
    runs = await _runs(session, members)
    route = await session.get(Route, group.route_id)
    hidden = is_mountain(route)
    run_stops: dict[UUID, list[RouteExecutionStop]] = {run_id: [] for run_id in runs}
    if runs:
        for stop in (
            await session.scalars(
                select(RouteExecutionStop)
                .where(RouteExecutionStop.execution_id.in_(list(runs)))
                .order_by(RouteExecutionStop.position)
            )
        ).all():
            run_stops[stop.execution_id].append(stop)
    offline = await _offline_stops(session, list(runs))

    # The stops as the group walks them: a run's own copy when there is one.
    template = next((stops for stops in run_stops.values() if stops), None)
    if template is not None:
        stop_rows = [(s.position, s.place_name, s.is_optional) for s in template]
    else:
        from tourism_backend.modules.places.infrastructure.models import Place

        stop_rows = [
            (position, name, optional)
            for position, name, optional in (
                await session.execute(
                    select(RouteStop.position, Place.name, RouteStop.is_optional)
                    .join(Place, Place.id == RouteStop.place_id)
                    .where(RouteStop.route_id == group.route_id)
                    .order_by(RouteStop.position)
                )
            ).all()
        ]

    user_ids = [m.user_id for m in members]
    users = {
        u.id: u for u in (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
    }
    avatars = await media_service.resolve_urls(
        session, entity_type="user", entity_ids=user_ids, role="avatar"
    )

    # First at each stop: the earliest mark made online (D7, D11).
    firsts: dict[int, tuple[datetime, UUID]] = {}
    member_rows: list[CompanionMemberProgressOut] = []
    finishers: list[tuple[datetime, UUID, RouteExecution]] = []
    for m in members:
        run = runs.get(m.execution_id) if m.execution_id is not None else None
        stops = run_stops.get(run.id, []) if run is not None else []
        marks = []
        last = 0
        for stop in stops:
            settled_at = stop.completed_at or stop.skipped_at
            if settled_at is not None:
                last = max(last, stop.position)
            was_offline = stop.id in offline
            if stop.completed_at is not None and not was_offline:
                seen = firsts.get(stop.position)
                if seen is None or stop.completed_at < seen[0]:
                    firsts[stop.position] = (stop.completed_at, m.user_id)
            if settled_at is not None:
                marks.append(
                    CompanionStopMarkOut(
                        position=stop.position,
                        marked_at=stop.completed_at,
                        skipped=stop.skipped_at is not None,
                        offline=was_offline,
                    )
                )
        reached = bool(run is not None and run.status == "completed" and run.counted)
        skipped_required = any(s.skipped_at is not None and not s.is_optional for s in stops)
        if reached and run is not None and run.completed_at is not None and not skipped_required:
            finishers.append((run.completed_at, m.user_id, run))
        user = users.get(m.user_id)
        member_rows.append(
            CompanionMemberProgressOut(
                user_id=str(m.user_id),
                display_name=user.display_name if user is not None else "",
                avatar_url=avatars.get(m.user_id),
                is_organizer=m.user_id == group.organizer_user_id,
                joined_late=m.joined_late,
                status=_status(group, m, run),  # type: ignore[arg-type]
                last_position=last,
                marks=marks,
                started_at=run.started_at if run is not None else None,
                finished_at=run.completed_at if run is not None else None,
                reached=reached,
            )
        )

    started = [runs[m.execution_id] for m in members if m.execution_id in runs]
    reached_runs = [run for run in started if run.status == "completed" and run.counted]
    last_arrival = max((run.completed_at for run in reached_runs if run.completed_at), default=None)
    paid = [run.paid_distance_meters for run in reached_runs if run.paid_distance_meters]
    finishers.sort(key=lambda row: row[0])
    summary = CompanionSummaryOut(
        settled=group.status == "settled",
        started_count=len(started),
        reached_count=len(reached_runs),
        everyone_reached=bool(started) and len(reached_runs) == len(members),
        distance_meters=max(paid) if paid else None,
        total_seconds=(
            int((last_arrival - group.started_at).total_seconds())
            if last_arrival is not None and group.started_at is not None
            else None
        ),
        first_finisher_user_id=(str(finishers[0][1]) if finishers and not hidden else None),
    )
    return CompanionProgressOut(
        group_id=str(group.id),
        status=group.status,  # type: ignore[arg-type]
        started_at=group.started_at,
        leaderboard_hidden=hidden,
        stops=[
            CompanionStopOut(
                position=position,
                place_name=name,
                is_optional=optional,
                first_user_id=(
                    str(firsts[position][1]) if position in firsts and not hidden else None
                ),
            )
            for position, name, optional in stop_rows
        ],
        members=member_rows,
        summary=summary,
    )


async def after_run_ended(
    session: AsyncSession, *, settings: Settings | None, execution_id: UUID
) -> None:
    """A member finished or gave up: the others hear of it, the result may be due."""
    member: CompanionMember | None = await session.scalar(
        select(CompanionMember).where(CompanionMember.execution_id == execution_id)
    )
    if member is None:
        return
    group = await session.get(CompanionGroup, member.group_id)
    run = await session.get(RouteExecution, execution_id)
    if group is None or run is None or group.status != "started":
        return
    user = await session.get(User, member.user_id)
    name = user.display_name if user is not None else "Попутчик"
    finished = run.status == "completed"
    others = [m.user_id for m in await _joined(session, group.id) if m.user_id != member.user_id]
    pushes = await groups._notify(
        session,
        settings,
        user_ids=others,
        kind="companion_member_finished" if finished else "companion_member_left",
        title="Попутчик дошёл" if finished else "Попутчик сошёл с маршрута",
        body=f"{name} завершил маршрут" if finished else f"{name} прекратил прохождение",
        group_id=group.id,
        actor_id=member.user_id,
    )
    await session.commit()
    await groups._push(session, settings, pushes, group.id)
    if finished:
        # «Компания» may now be true for those who finished earlier.
        from tourism_backend.modules.achievements.service import after_commit

        for other in others:
            await after_commit(session, other, {"completion"})
    await settle_if_due(session, group)


async def company_count(session: AsyncSession, user_id: UUID) -> int:
    """Groups that make «Компания» (D16): this person's run and another
    member's both counted, with a shared stop marked within an hour."""
    mine = (
        await session.execute(
            select(CompanionMember.group_id, RouteExecution.id)
            .join(RouteExecution, RouteExecution.id == CompanionMember.execution_id)
            .where(
                CompanionMember.user_id == user_id,
                CompanionMember.state == "joined",
                RouteExecution.status == "completed",
                RouteExecution.counted.is_(True),
            )
        )
    ).all()
    count = 0
    for group_id, run_id in mine:
        others = (
            await session.scalars(
                select(RouteExecution.id)
                .join(CompanionMember, CompanionMember.execution_id == RouteExecution.id)
                .where(
                    CompanionMember.group_id == group_id,
                    CompanionMember.user_id != user_id,
                    CompanionMember.state == "joined",
                    RouteExecution.status == "completed",
                    RouteExecution.counted.is_(True),
                )
            )
        ).all()
        if not others:
            continue
        own_marks = {
            place: at
            for place, at in (
                await session.execute(
                    select(RouteExecutionStop.place_id, RouteExecutionStop.completed_at).where(
                        RouteExecutionStop.execution_id == run_id,
                        RouteExecutionStop.completed_at.is_not(None),
                    )
                )
            ).all()
            if place is not None and at is not None
        }
        other_marks = (
            await session.execute(
                select(RouteExecutionStop.place_id, RouteExecutionStop.completed_at).where(
                    RouteExecutionStop.execution_id.in_(list(others)),
                    RouteExecutionStop.completed_at.is_not(None),
                )
            )
        ).all()
        if any(
            place in own_marks and at is not None and abs(own_marks[place] - at) <= COMPANY_WINDOW
            for place, at in other_marks
        ):
            count += 1
    return count
