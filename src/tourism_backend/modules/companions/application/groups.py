"""Groups of companions and their invitations (BACKEND-59, spec 11b).

Only the organizer invites, removes and answers code requests; an invited
person accepts or declines. Invitations come from mutual followers only and
never across a block (``identity.application.invitations``). A person is in
one live group at a time: they can walk one route at a time anyway.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.companions.application.schemas import (
    CompanionGroupListOut,
    CompanionGroupOut,
    CompanionMemberOut,
    CompanionReportIn,
)
from tourism_backend.modules.companions.infrastructure.models import (
    CompanionGroup,
    CompanionMember,
)
from tourism_backend.modules.identity.application import invitations
from tourism_backend.modules.identity.application.bean_hats import effective_looks, rank_titles
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.moderation.infrastructure.models import ContentReport
from tourism_backend.modules.notifications.application import service as notifications
from tourism_backend.modules.routes.application.route_catalog import cover_urls_for_routes
from tourism_backend.modules.routes.infrastructure.models import Route
from tourism_backend.modules.subscriptions.application.entitlements import policy_for_user

INVITE_TTL = timedelta(hours=48)
#: Invitations one person may send in a day (D10).
DAILY_INVITE_LIMIT = 30
#: Codes one person may try in an hour before they have to wait (D15).
CODE_ATTEMPTS_PER_HOUR = 10
CODE_LENGTH = 6
# No 0/O, 1/I/L: the code is read aloud and typed from another phone.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

LIVE_STATUSES = ("gathering", "started")
#: States that hold a seat in the group.
_SEATED = ("joined", "invited", "requested")


def _now() -> datetime:
    return datetime.now(UTC)


def _not_found() -> AppError:
    return AppError(code="companion_group_not_found", message="Группа не найдена", status_code=404)


def _conflict(code: str, message: str) -> AppError:
    return AppError(code=code, message=message, status_code=409)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise _not_found() from error


def _invite_live(member: CompanionMember, now: datetime) -> bool:
    return member.state == "invited" and (member.expires_at is None or member.expires_at > now)


def _holds_seat(member: CompanionMember, now: datetime) -> bool:
    if member.state == "invited":
        return _invite_live(member, now)
    return member.state in _SEATED


async def _members(session: AsyncSession, group_id: UUID) -> list[CompanionMember]:
    return list(
        (
            await session.scalars(
                select(CompanionMember)
                .where(CompanionMember.group_id == group_id)
                .order_by(CompanionMember.created_at, CompanionMember.id)
            )
        ).all()
    )


async def _member(session: AsyncSession, group_id: UUID, user_id: UUID) -> CompanionMember | None:
    member: CompanionMember | None = await session.scalar(
        select(CompanionMember).where(
            CompanionMember.group_id == group_id, CompanionMember.user_id == user_id
        )
    )
    return member


def _route_open(route: Route | None) -> bool:
    """A published catalogue route a group can walk (D13)."""
    return (
        route is not None
        and route.publication_status == "published"
        and route.visibility == "public"
        and route.lifecycle_status == "active"
        and route.revision_of_route_id is None
    )


async def _live_membership(
    session: AsyncSession, user_id: UUID, *, except_group: UUID | None = None
) -> CompanionGroup | None:
    query = (
        select(CompanionGroup)
        .join(CompanionMember, CompanionMember.group_id == CompanionGroup.id)
        .where(
            CompanionMember.user_id == user_id,
            CompanionMember.state == "joined",
            CompanionGroup.status.in_(LIVE_STATUSES),
        )
    )
    if except_group is not None:
        query = query.where(CompanionGroup.id != except_group)
    group: CompanionGroup | None = await session.scalar(query.limit(1))
    return group


async def _ensure_free(session: AsyncSession, user_id: UUID, *, group_id: UUID | None) -> None:
    if await _live_membership(session, user_id, except_group=group_id) is not None:
        raise _conflict(
            "companion_already_in_group",
            "Вы уже в другой группе. Сначала выйдите из неё.",
        )


async def _new_code(session: AsyncSession) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
        taken = await session.scalar(
            select(CompanionGroup.id).where(
                CompanionGroup.join_code == code, CompanionGroup.status.in_(LIVE_STATUSES)
            )
        )
        if taken is None:
            return code
    raise AppError(code="companion_code_busy", message="Попробуйте ещё раз", status_code=503)


async def _notify(
    session: AsyncSession,
    settings: Settings | None,
    *,
    user_ids: list[UUID],
    kind: str,
    title: str,
    body: str,
    group_id: UUID,
    actor_id: UUID | None,
) -> list[tuple[UUID, str, str, str]]:
    """Inbox rows now; the pushes go out after the commit (see ``_push``)."""
    pushes: list[tuple[UUID, str, str, str]] = []
    for user_id in user_ids:
        if user_id == actor_id:
            continue
        await notifications.create_companion_notification(
            session,
            recipient_user_id=user_id,
            actor_user_id=actor_id,
            kind=kind,
            title=title,
            body=body,
            group_id=group_id,
        )
        pushes.append((user_id, kind, title, body))
    del settings
    return pushes


async def _push(
    session: AsyncSession,
    settings: Settings | None,
    pushes: list[tuple[UUID, str, str, str]],
    group_id: UUID,
) -> None:
    if settings is None:
        return
    for user_id, kind, title, body in pushes:
        await notifications.maybe_push_notification(
            session,
            settings,
            user_id=user_id,
            kind=kind,
            title=title,
            body=body,
            target_type="companion_group",
            target_id=group_id,
        )


async def _cancel(
    session: AsyncSession,
    group: CompanionGroup,
    *,
    reason: str,
    actor_id: UUID | None,
    body: str,
) -> list[tuple[UUID, str, str, str]]:
    now = _now()
    group.status = "cancelled"
    group.cancelled_at = now
    group.cancel_reason = reason
    people = [
        member.user_id for member in await _members(session, group.id) if _holds_seat(member, now)
    ]
    return await _notify(
        session,
        None,
        user_ids=people,
        kind="companion_group_cancelled",
        title="Совместный маршрут отменён",
        body=body,
        group_id=group.id,
        actor_id=actor_id,
    )


async def _group_for(session: AsyncSession, group_id: str) -> CompanionGroup:
    group = await session.get(CompanionGroup, _parse_uuid(group_id))
    if group is None:
        raise _not_found()
    return group


async def _check_route_still_open(
    session: AsyncSession, settings: Settings | None, group: CompanionGroup
) -> None:
    """A route taken off the catalogue cancels a group that has not left (D13)."""
    if group.status != "gathering":
        return
    if _route_open(await session.get(Route, group.route_id)):
        return
    pushes = await _cancel(
        session,
        group,
        reason="route_unavailable",
        actor_id=None,
        body="Маршрут сняли с публикации, поэтому группа отменена.",
    )
    await session.commit()
    await _push(session, settings, pushes, group.id)


async def cancel_groups_for_route(
    session: AsyncSession, route_id: UUID, *, settings: Settings | None = None
) -> None:
    """Called where a route leaves the catalogue; the caller commits."""
    groups = (
        await session.scalars(
            select(CompanionGroup).where(
                CompanionGroup.route_id == route_id, CompanionGroup.status == "gathering"
            )
        )
    ).all()
    for group in groups:
        await _cancel(
            session,
            group,
            reason="route_unavailable",
            actor_id=None,
            body="Маршрут сняли с публикации, поэтому группа отменена.",
        )
    del settings


async def _out(session: AsyncSession, group: CompanionGroup, viewer_id: UUID) -> CompanionGroupOut:
    now = _now()
    members = await _members(session, group.id)
    mine = next((m for m in members if m.user_id == viewer_id), None)
    is_organizer = group.organizer_user_id == viewer_id
    # An organizer who left the group on the way is out like anyone else.
    inside = (mine is not None and mine.state == "joined") or (is_organizer and mine is None)
    shown = [
        m
        for m in members
        if (inside and _holds_seat(m, now)) or m.user_id == group.organizer_user_id
    ]
    user_ids = [m.user_id for m in shown]
    users = {
        user.id: user
        for user in (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
    }
    avatars = await media_service.resolve_urls(
        session, entity_type="user", entity_ids=user_ids, role="avatar"
    )
    looks = await effective_looks(session, list(users.values()))
    ranks = await rank_titles(session, list(users.values()))
    route = await session.get(Route, group.route_id)
    covers = await cover_urls_for_routes(session, [group.route_id])
    organizer = users.get(group.organizer_user_id) or await session.get(
        User, group.organizer_user_id
    )
    return CompanionGroupOut(
        id=str(group.id),
        route_id=str(group.route_id),
        route_name=route.name if route is not None else "",
        route_cover_url=covers.get(group.route_id),
        status=group.status,  # type: ignore[arg-type]
        organizer_user_id=str(group.organizer_user_id),
        organizer_display_name=organizer.display_name if organizer is not None else "",
        is_organizer=is_organizer,
        max_members=group.max_members,
        my_state=mine.state if mine is not None else None,  # type: ignore[arg-type]
        join_code=group.join_code if inside and group.status in LIVE_STATUSES else None,
        members=[
            CompanionMemberOut(
                user_id=str(m.user_id),
                display_name=users[m.user_id].display_name if m.user_id in users else "",
                avatar_url=avatars.get(m.user_id),
                bean_hat=looks[m.user_id].hat if m.user_id in looks else None,
                bean_kind=looks[m.user_id].kind if m.user_id in looks else None,
                bean_outfit=looks[m.user_id].outfit if m.user_id in looks else None,
                rank_title=ranks.get(m.user_id),
                state=m.state,  # type: ignore[arg-type]
                is_organizer=m.user_id == group.organizer_user_id,
                ready=m.ready,
                joined_late=m.joined_late,
                started=m.execution_id is not None,
                invite_expires_at=m.expires_at if m.state == "invited" else None,
            )
            for m in shown
        ],
        joined_count=sum(1 for m in members if m.state == "joined"),
        created_at=group.created_at,
        started_at=group.started_at,
        settled_at=group.settled_at,
        cancelled_at=group.cancelled_at,
        cancel_reason=group.cancel_reason,
    )


async def _visible_group(
    session: AsyncSession, group_id: str, viewer_id: UUID
) -> tuple[CompanionGroup, CompanionMember | None]:
    group = await _group_for(session, group_id)
    member = await _member(session, group.id, viewer_id)
    if member is None or member.state in ("revoked", "rejected", "removed"):
        raise _not_found()
    if member.state == "invited" and not _invite_live(member, _now()):
        raise _not_found()
    return group, member


def _require_organizer(group: CompanionGroup, user_id: UUID) -> None:
    if group.organizer_user_id != user_id:
        raise AppError(
            code="companion_not_organizer",
            message="Это может только организатор",
            status_code=403,
        )


def _require_gathering(group: CompanionGroup) -> None:
    if group.status != "gathering":
        raise _conflict("companion_group_closed", "Группа уже вышла на маршрут или закрыта")


async def create_group(session: AsyncSession, *, user_id: UUID, route_id: str) -> CompanionGroupOut:
    route = await session.get(Route, _parse_uuid(route_id))
    if not _route_open(route):
        raise _conflict(
            "companion_route_not_public",
            "Совместно можно пройти только опубликованный маршрут из каталога",
        )
    assert route is not None
    await _ensure_free(session, user_id, group_id=None)
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    now = _now()
    group = CompanionGroup(
        route_id=route.id,
        organizer_user_id=user_id,
        status="gathering",
        max_members=policy_for_user(user).max_group_members,
        join_code=await _new_code(session),
        route_version_at=route.content_updated_at,
        created_at=now,
        updated_at=now,
    )
    session.add(group)
    await session.flush()
    session.add(
        CompanionMember(
            group_id=group.id,
            user_id=user_id,
            state="joined",
            responded_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    return await _out(session, group, user_id)


async def get_group(
    session: AsyncSession, *, settings: Settings | None, group_id: str, user_id: UUID
) -> CompanionGroupOut:
    group, _member_row = await _visible_group(session, group_id, user_id)
    await _check_route_still_open(session, settings, group)
    return await _out(session, group, user_id)


async def list_my_groups(session: AsyncSession, *, user_id: UUID) -> CompanionGroupListOut:
    """Live groups I am in or invited to, then the last finished ones."""
    now = _now()
    rows = (
        await session.execute(
            select(CompanionGroup, CompanionMember)
            .join(CompanionMember, CompanionMember.group_id == CompanionGroup.id)
            .where(
                CompanionMember.user_id == user_id,
                CompanionMember.state.in_(("joined", "invited", "requested")),
            )
            .order_by(CompanionGroup.created_at.desc())
            .limit(30)
        )
    ).all()
    items = []
    for group, member in rows:
        if member.state == "invited" and (
            not _invite_live(member, now) or group.status not in LIVE_STATUSES
        ):
            continue
        items.append(await _out(session, group, user_id))
    return CompanionGroupListOut(items=items)


async def _seats_taken(session: AsyncSession, group: CompanionGroup) -> int:
    now = _now()
    return sum(1 for m in await _members(session, group.id) if _holds_seat(m, now))


async def _ensure_seat(session: AsyncSession, group: CompanionGroup) -> None:
    if await _seats_taken(session, group) >= group.max_members:
        raise _conflict("companion_group_full", "В группе нет свободных мест")


async def invite(
    session: AsyncSession,
    *,
    settings: Settings | None,
    group_id: str,
    actor_id: UUID,
    invitee_id: str,
) -> CompanionGroupOut:
    group = await _group_for(session, group_id)
    _require_organizer(group, actor_id)
    await _check_route_still_open(session, settings, group)
    _require_gathering(group)
    target = _parse_uuid(invitee_id)
    await invitations.ensure_can_invite(session, inviter_id=actor_id, invitee_id=target)
    now = _now()
    existing = await _member(session, group.id, target)
    if existing is not None:
        if existing.state == "joined" or _invite_live(existing, now):
            return await _out(session, group, actor_id)
        if existing.state == "declined":
            # Not sent again into the same group (D10).
            raise _conflict(
                "companion_invite_declined", "Этот человек уже отказался от этой группы"
            )
    sent_today = int(
        await session.scalar(
            select(func.count())
            .select_from(CompanionMember)
            .where(
                CompanionMember.invited_by_user_id == actor_id,
                CompanionMember.created_at > now - timedelta(days=1),
            )
        )
        or 0
    )
    if sent_today >= DAILY_INVITE_LIMIT:
        raise AppError(
            code="rate_limited",
            message="Слишком много приглашений за сутки. Попробуйте завтра.",
            status_code=429,
        )
    await _ensure_seat(session, group)
    if existing is None:
        session.add(
            CompanionMember(
                group_id=group.id,
                user_id=target,
                state="invited",
                invited_by_user_id=actor_id,
                expires_at=now + INVITE_TTL,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        existing.state = "invited"
        existing.invited_by_user_id = actor_id
        existing.expires_at = now + INVITE_TTL
        existing.responded_at = None
        existing.ready = False
        existing.updated_at = now
    route = await session.get(Route, group.route_id)
    organizer = await session.get(User, actor_id)
    pushes = await _notify(
        session,
        settings,
        user_ids=[target],
        kind="companion_invite",
        title="Приглашение пройти маршрут вместе",
        body=(
            f"{organizer.display_name if organizer else 'Вас'} зовёт пройти "
            f"«{route.name if route else 'маршрут'}» вместе"
        ),
        group_id=group.id,
        actor_id=actor_id,
    )
    await session.commit()
    await _push(session, settings, pushes, group.id)
    return await _out(session, group, actor_id)


async def respond(
    session: AsyncSession,
    *,
    settings: Settings | None,
    group_id: str,
    user_id: UUID,
    accept: bool,
) -> CompanionGroupOut:
    group, member = await _visible_group(session, group_id, user_id)
    assert member is not None
    if member.state != "invited":
        raise _conflict("companion_no_invite", "Приглашения нет или оно уже не действует")
    await _check_route_still_open(session, settings, group)
    _require_gathering(group)
    if await invitations.blocked_between(session, user_id, group.organizer_user_id):
        raise _not_found()
    now = _now()
    member.responded_at = now
    member.updated_at = now
    if not accept:
        member.state = "declined"
        await session.commit()
        return await _out(session, group, user_id)
    await _ensure_free(session, user_id, group_id=group.id)
    member.state = "joined"
    await session.commit()
    return await _out(session, group, user_id)


async def join_by_code(
    session: AsyncSession,
    redis: Redis,
    *,
    settings: Settings | None,
    user_id: UUID,
    code: str,
) -> CompanionGroupOut:
    """Asks to join; the organizer always confirms a code (D15)."""
    key = f"companions:code:{user_id}"
    attempts = await redis.incr(key)
    if attempts == 1:
        await redis.expire(key, 3600)
    if attempts > CODE_ATTEMPTS_PER_HOUR:
        raise AppError(
            code="rate_limited",
            message="Слишком много попыток. Попробуйте через час.",
            status_code=429,
        )
    cleaned = "".join(code.split()).upper()
    group: CompanionGroup | None = await session.scalar(
        select(CompanionGroup).where(
            CompanionGroup.join_code == cleaned, CompanionGroup.status.in_(LIVE_STATUSES)
        )
    )
    # A block reads as a wrong code: the code does not tell who is behind it.
    if group is None or await invitations.blocked_between(
        session, user_id, group.organizer_user_id
    ):
        raise AppError(code="companion_code_invalid", message="Код не подошёл", status_code=404)
    await _check_route_still_open(session, settings, group)
    if group.status not in LIVE_STATUSES:
        raise AppError(code="companion_code_invalid", message="Код не подошёл", status_code=404)
    existing = await _member(session, group.id, user_id)
    if existing is not None and existing.state in ("joined", "requested"):
        return await _out(session, group, user_id)
    await _ensure_free(session, user_id, group_id=group.id)
    await _ensure_seat(session, group)
    now = _now()
    if existing is None:
        existing = CompanionMember(
            group_id=group.id,
            user_id=user_id,
            state="requested",
            created_at=now,
            updated_at=now,
        )
        session.add(existing)
    else:
        existing.state = "requested"
        existing.expires_at = None
        existing.responded_at = None
        existing.updated_at = now
    requester = await session.get(User, user_id)
    pushes = await _notify(
        session,
        settings,
        user_ids=[group.organizer_user_id],
        kind="companion_join_request",
        title="Просятся в группу",
        body=f"{requester.display_name if requester else 'Кто-то'} ввёл код вашей группы",
        group_id=group.id,
        actor_id=user_id,
    )
    await session.commit()
    await _push(session, settings, pushes, group.id)
    return await _out(session, group, user_id)


async def answer_request(
    session: AsyncSession,
    *,
    group_id: str,
    actor_id: UUID,
    user_id: str,
    approve: bool,
) -> CompanionGroupOut:
    group = await _group_for(session, group_id)
    _require_organizer(group, actor_id)
    if group.status not in LIVE_STATUSES:
        raise _conflict("companion_group_closed", "Группа уже закрыта")
    member = await _member(session, group.id, _parse_uuid(user_id))
    if member is None or member.state != "requested":
        raise _conflict("companion_no_request", "Запроса нет")
    now = _now()
    member.responded_at = now
    member.updated_at = now
    if approve:
        await _ensure_free(session, member.user_id, group_id=group.id)
        member.state = "joined"
        # After the start a newcomer walks from their own start (D5).
        member.joined_late = group.status == "started"
    else:
        member.state = "rejected"
    await session.commit()
    return await _out(session, group, actor_id)


async def remove_member(
    session: AsyncSession,
    *,
    settings: Settings | None,
    group_id: str,
    actor_id: UUID,
    user_id: str,
) -> CompanionGroupOut | None:
    """The organizer takes back an invitation or removes someone; anyone else
    may only leave. Before the start the organizer leaves by cancelling the
    group; on the way, when cancelling is no longer possible, the organizer
    may leave like anyone else (BACKEND-72)."""
    group = await _group_for(session, group_id)
    target = _parse_uuid(user_id)
    member = await _member(session, group.id, target)
    if member is None:
        raise _not_found()
    now = _now()
    if target == actor_id:
        if target == group.organizer_user_id and group.status != "started":
            raise _conflict(
                "companion_organizer_leaves",
                "Организатор не выходит из группы, а отменяет её",
            )
        if member.state not in ("joined", "requested"):
            raise _not_found()
        member.state = "left"
        member.ready = False
        member.updated_at = now
        pushes: list[tuple[UUID, str, str, str]] = []
        if group.status == "started":
            leaver = await session.get(User, actor_id)
            pushes = await _notify(
                session,
                settings,
                user_ids=[
                    m.user_id
                    for m in await _members(session, group.id)
                    if m.state == "joined" and m.user_id != actor_id
                ],
                kind="companion_member_left",
                title="Попутчик вышел из группы",
                body=f"{leaver.display_name if leaver else 'Попутчик'} вышел из группы",
                group_id=group.id,
                actor_id=actor_id,
            )
        await session.commit()
        await _push(session, settings, pushes, group.id)
        return None
    _require_organizer(group, actor_id)
    if target == group.organizer_user_id:
        raise _conflict("companion_organizer_leaves", "Организатора нельзя убрать")
    member.state = {"invited": "revoked", "requested": "rejected"}.get(member.state, "removed")
    member.ready = False
    member.updated_at = now
    await session.commit()
    return await _out(session, group, actor_id)


async def cancel_group(
    session: AsyncSession, *, settings: Settings | None, group_id: str, actor_id: UUID
) -> CompanionGroupOut:
    group = await _group_for(session, group_id)
    _require_organizer(group, actor_id)
    _require_gathering(group)
    organizer = await session.get(User, actor_id)
    pushes = await _cancel(
        session,
        group,
        reason="organizer",
        actor_id=actor_id,
        body=f"{organizer.display_name if organizer else 'Организатор'} отменил совместный маршрут",
    )
    await session.commit()
    await _push(session, settings, pushes, group.id)
    return await _out(session, group, actor_id)


async def report_invitation(
    session: AsyncSession, *, group_id: str, user_id: UUID, payload: CompanionReportIn
) -> None:
    """A complaint about an invitation, in its own moderation queue (D10)."""
    group = await _group_for(session, group_id)
    member = await _member(session, group.id, user_id)
    if member is None or user_id == group.organizer_user_id:
        raise _not_found()
    existing = await session.scalar(
        select(ContentReport.id).where(
            ContentReport.target_type == "companion_group",
            ContentReport.target_id == group.id,
            ContentReport.reporter_user_id == user_id,
        )
    )
    if existing is None:
        now = _now()
        session.add(
            ContentReport(
                target_type="companion_group",
                target_id=group.id,
                reporter_user_id=user_id,
                reason=payload.reason,
                comment=(payload.comment or "").strip() or None,
                status="new",
                created_at=now,
                updated_at=now,
            )
        )
    # Reporting an invitation also declines it.
    if member.state == "invited":
        member.state = "declined"
        member.responded_at = _now()
    await session.commit()
