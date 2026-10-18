"""Co-authors of a user route, as the designer drew it (spec 11c, D7).

The owner picks up to three co-authors on the publish page; each gets
«… предлагает вам соавторство», opens the same publish page, may edit the
route and presses «Опубликовать» or declines. «Опубликовать» is a consent to
the route as it is now: the route goes to the moderator once the owner and
every co-author consented, and any edit clears the consents given so far.

Awards stay the owner's; the «own route» rules cover everyone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.identity.application.bean_hats import effective_hat, effective_kind
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.notifications.application import service as notifications
from tourism_backend.modules.routes.application.schemas import RouteCoauthorOut
from tourism_backend.modules.routes.infrastructure.models import Route, RouteCoauthor

MAX_COAUTHORS = 3
#: Still in it: invited and not answered yet, or joined.
ACTIVE = ("invited", "joined")

Role = Literal["owner", "coauthor"]


def _now() -> datetime:
    return datetime.now(UTC)


async def rows(session: AsyncSession, route_id: UUID) -> list[RouteCoauthor]:
    return list(
        (
            await session.scalars(
                select(RouteCoauthor)
                .where(RouteCoauthor.route_id == route_id)
                .order_by(RouteCoauthor.created_at, RouteCoauthor.id)
            )
        ).all()
    )


async def active_rows(session: AsyncSession, route_id: UUID) -> list[RouteCoauthor]:
    return [row for row in await rows(session, route_id) if row.state in ACTIVE]


async def role_on(session: AsyncSession, route: Route, user_id: UUID) -> Role | None:
    if route.owner_user_id == user_id:
        return "owner"
    found = await session.scalar(
        select(RouteCoauthor.id).where(
            RouteCoauthor.route_id == route.id,
            RouteCoauthor.user_id == user_id,
            RouteCoauthor.state.in_(ACTIVE),
        )
    )
    return "coauthor" if found is not None else None


async def is_author(session: AsyncSession, route_id: UUID, user_id: UUID) -> bool:
    """The owner or a joined co-author: «own route» rules apply (D2)."""
    route = await session.get(Route, route_id)
    if route is None:
        return False
    if route.owner_user_id == user_id:
        return True
    found = await session.scalar(
        select(RouteCoauthor.id).where(
            RouteCoauthor.route_id == route_id,
            RouteCoauthor.user_id == user_id,
            RouteCoauthor.state == "joined",
        )
    )
    return found is not None


async def coauthored_route_ids(
    session: AsyncSession, user_id: UUID, *, states: tuple[str, ...] = ACTIVE
) -> list[UUID]:
    return list(
        (
            await session.scalars(
                select(RouteCoauthor.route_id).where(
                    RouteCoauthor.user_id == user_id, RouteCoauthor.state.in_(states)
                )
            )
        ).all()
    )


async def reset_consents(session: AsyncSession, route: Route) -> None:
    """An edit: everyone agrees again to the version that will be sent."""
    route.owner_consented_at = None
    await session.execute(
        update(RouteCoauthor)
        .where(RouteCoauthor.route_id == route.id, RouteCoauthor.consented_at.is_not(None))
        .values(consented_at=None)
    )


async def everyone_consented(session: AsyncSession, route: Route) -> bool:
    if route.owner_consented_at is None:
        return False
    return all(
        row.state == "joined" and row.consented_at is not None
        for row in await active_rows(session, route.id)
    )


async def record_consent(session: AsyncSession, route: Route, user_id: UUID) -> None:
    now = _now()
    if route.owner_user_id == user_id:
        route.owner_consented_at = now
        return
    row = await session.scalar(
        select(RouteCoauthor).where(
            RouteCoauthor.route_id == route.id,
            RouteCoauthor.user_id == user_id,
            RouteCoauthor.state.in_(ACTIVE),
        )
    )
    if row is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    row.state = "joined"
    row.consented_at = now
    row.updated_at = now


async def coauthors_out(
    session: AsyncSession,
    route_ids: list[UUID],
    *,
    states: tuple[str, ...] = ("joined",),
) -> dict[UUID, list[RouteCoauthorOut]]:
    if not route_ids:
        return {}
    found = list(
        (
            await session.scalars(
                select(RouteCoauthor)
                .where(RouteCoauthor.route_id.in_(route_ids), RouteCoauthor.state.in_(states))
                .order_by(RouteCoauthor.created_at, RouteCoauthor.id)
            )
        ).all()
    )
    if not found:
        return {}
    user_ids = list({row.user_id for row in found})
    users = {
        user.id: user
        for user in (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
    }
    avatars = await media_service.resolve_urls(
        session, entity_type="user", entity_ids=user_ids, role="avatar"
    )
    result: dict[UUID, list[RouteCoauthorOut]] = {}
    for row in found:
        user = users.get(row.user_id)
        result.setdefault(row.route_id, []).append(
            RouteCoauthorOut(
                user_id=row.user_id,
                display_name=user.display_name if user is not None else "",
                avatar_url=avatars.get(row.user_id),
                bean_kind=effective_kind(user) if user is not None else None,
                bean_hat=effective_hat(user, set()) if user is not None else None,
                state=row.state,  # type: ignore[arg-type]
                consented=row.consented_at is not None,
            )
        )
    return result


async def _notify(
    session: AsyncSession,
    settings: Settings | None,
    *,
    user_id: UUID,
    actor_id: UUID,
    kind: str,
    title: str,
    body: str,
    route_id: UUID,
) -> None:
    await notifications.create_route_coauthor_notification(
        session,
        recipient_user_id=user_id,
        actor_user_id=actor_id,
        kind=kind,
        title=title,
        body=body,
        route_id=route_id,
    )
    await session.commit()
    if settings is not None:
        await notifications.maybe_push_notification(
            session,
            settings,
            user_id=user_id,
            kind=kind,
            title=title,
            body=body,
            target_type="route",
            target_id=route_id,
        )


async def set_coauthors(
    session: AsyncSession,
    *,
    settings: Settings | None,
    route_id: UUID,
    owner_id: UUID,
    user_ids: list[UUID],
) -> list[RouteCoauthorOut]:
    """The owner's choice on the publish page; the list replaces the old one."""
    route = await session.get(Route, route_id)
    if (
        route is None
        or route.owner_user_id != owner_id
        or route.source not in {"user_created", "generated"}
        or route.revision_of_route_id is not None
        or route.publication_status == "deleted"
    ):
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    wanted = list(dict.fromkeys(user_ids))
    if len(wanted) > MAX_COAUTHORS:
        raise AppError(
            code="route_coauthors_limit",
            message=f"Соавторов может быть не больше {MAX_COAUTHORS}",
            status_code=409,
        )
    existing = {row.user_id: row for row in await rows(session, route.id)}
    newcomers = [
        user_id
        for user_id in wanted
        if user_id not in existing or existing[user_id].state not in ACTIVE
    ]
    # Imported here: identity's public profiles list routes through this module.
    from tourism_backend.modules.identity.application import invitations

    for user_id in newcomers:
        await invitations.ensure_can_invite(session, inviter_id=owner_id, invitee_id=user_id)
    now = _now()
    changed = bool(newcomers)
    for user_id, row in existing.items():
        if row.state in ACTIVE and user_id not in wanted:
            row.state = "removed"
            row.consented_at = None
            row.updated_at = now
            changed = True
    for user_id in newcomers:
        previous = existing.get(user_id)
        if previous is None:
            session.add(
                RouteCoauthor(
                    route_id=route.id,
                    user_id=user_id,
                    state="invited",
                    invited_by_user_id=owner_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            previous.state = "invited"
            previous.invited_by_user_id = owner_id
            previous.consented_at = None
            previous.updated_at = now
    if changed:
        # Whom the route is published with is part of what everyone agrees to.
        await reset_consents(session, route)
    await session.commit()
    owner = await session.get(User, owner_id)
    for user_id in newcomers:
        await _notify(
            session,
            settings,
            user_id=user_id,
            actor_id=owner_id,
            kind="route_coauthor_invite",
            title=f"{owner.display_name if owner else 'Вас'} предлагает вам соавторство",
            body=f"Маршрут «{route.name}» ожидает вашего согласия на публикацию",
            route_id=route.id,
        )
    return (await coauthors_out(session, [route.id], states=ACTIVE)).get(route.id, [])


async def leave(
    session: AsyncSession, *, settings: Settings | None, route_id: UUID, user_id: UUID
) -> None:
    """A co-author declines the invitation or steps out; their edits stay."""
    row = await session.scalar(
        select(RouteCoauthor).where(
            RouteCoauthor.route_id == route_id,
            RouteCoauthor.user_id == user_id,
            RouteCoauthor.state.in_(ACTIVE),
        )
    )
    route = await session.get(Route, route_id)
    if row is None or route is None:
        raise AppError(code="route_not_found", message="Route not found", status_code=404)
    row.state = "declined" if row.state == "invited" else "left"
    row.consented_at = None
    row.updated_at = _now()
    await session.commit()
    if route.owner_user_id is not None:
        user = await session.get(User, user_id)
        await _notify(
            session,
            settings,
            user_id=route.owner_user_id,
            actor_id=user_id,
            kind="route_coauthor_declined",
            title="Соавтор отказался",
            body=(
                f"{user.display_name if user else 'Соавтор'} не будет соавтором "
                f"маршрута «{route.name}»"
            ),
            route_id=route.id,
        )
