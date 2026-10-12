"""Who may invite whom: blocks and the «кто может меня приглашать» setting.

Spec 11b (D10, D15) and 11c (D4): a person is invited to a group of
companions or to co-author a route only by a mutual follower, only if they
accept invites, and never across a block in either direction. The two
features ask the same question here, so the rule lives in one place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.application import public_service
from tourism_backend.modules.identity.application.public_schemas import PublicUserListOut
from tourism_backend.modules.identity.infrastructure.models import ProfileLike, User, UserBlock
from tourism_backend.modules.media.application import service as media_service

InviteRefusal = Literal["self", "not_found", "blocked", "invites_closed", "not_mutual"]

#: What the app shows for each refusal.
INVITE_REFUSAL_MESSAGES: dict[str, str] = {
    "self": "Нельзя пригласить самого себя",
    "not_found": "Пользователь не найден",
    "blocked": "Этого человека нельзя пригласить",
    "invites_closed": "Этот человек не принимает приглашения",
    "not_mutual": "Пригласить можно только того, на кого вы подписаны взаимно",
}


async def _target(session: AsyncSession, user_id: UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    return user


async def block_user(session: AsyncSession, *, actor_id: UUID, target_user_id: UUID) -> None:
    if actor_id == target_user_id:
        raise AppError(code="validation_error", message="Cannot block yourself", status_code=400)
    await _target(session, target_user_id)
    if await session.get(UserBlock, (actor_id, target_user_id)) is None:
        session.add(
            UserBlock(
                blocker_id=actor_id,
                blocked_user_id=target_user_id,
                created_at=datetime.now(UTC),
            )
        )
    await session.commit()


async def unblock_user(session: AsyncSession, *, actor_id: UUID, target_user_id: UUID) -> None:
    await session.execute(
        delete(UserBlock).where(
            UserBlock.blocker_id == actor_id,
            UserBlock.blocked_user_id == target_user_id,
        )
    )
    await session.commit()


async def is_blocked_by(session: AsyncSession, *, viewer_id: UUID, user_id: UUID) -> bool:
    """The viewer blocked this person (the profile shows «Разблокировать»)."""
    return await session.get(UserBlock, (viewer_id, user_id)) is not None


def _blocked_between(a: Any, b: Any):  # type: ignore[no-untyped-def]
    return exists().where(
        or_(
            and_(UserBlock.blocker_id == a, UserBlock.blocked_user_id == b),
            and_(UserBlock.blocker_id == b, UserBlock.blocked_user_id == a),
        )
    )


async def blocked_between(session: AsyncSession, a: UUID, b: UUID) -> bool:
    return bool(await session.scalar(select(_blocked_between(a, b))))


async def is_mutual(session: AsyncSession, a: UUID, b: UUID) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(ProfileLike)
        .where(
            or_(
                and_(ProfileLike.liker_id == a, ProfileLike.liked_user_id == b),
                and_(ProfileLike.liker_id == b, ProfileLike.liked_user_id == a),
            )
        )
    )
    return count == 2


async def invite_refusal(
    session: AsyncSession, *, inviter_id: UUID, invitee_id: UUID
) -> InviteRefusal | None:
    """Why [inviter_id] may not invite [invitee_id], or None when they may."""
    if inviter_id == invitee_id:
        return "self"
    invitee = await session.get(User, invitee_id)
    if invitee is None:
        return "not_found"
    if await blocked_between(session, inviter_id, invitee_id):
        return "blocked"
    if invitee.invite_policy == "nobody":
        return "invites_closed"
    if not await is_mutual(session, inviter_id, invitee_id):
        return "not_mutual"
    return None


async def ensure_can_invite(session: AsyncSession, *, inviter_id: UUID, invitee_id: UUID) -> None:
    refusal = await invite_refusal(session, inviter_id=inviter_id, invitee_id=invitee_id)
    if refusal is not None:
        raise AppError(
            code=f"invite_{refusal}",
            message=INVITE_REFUSAL_MESSAGES[refusal],
            status_code=404 if refusal == "not_found" else 409,
        )


async def list_blocked(session: AsyncSession, *, actor_id: UUID) -> PublicUserListOut:
    users = list(
        (
            await session.scalars(
                select(User)
                .join(UserBlock, UserBlock.blocked_user_id == User.id)
                .where(UserBlock.blocker_id == actor_id)
                .order_by(UserBlock.created_at.desc(), User.id)
            )
        ).all()
    )
    return await _user_list(session, users)


async def list_invitable(session: AsyncSession, *, actor_id: UUID) -> PublicUserListOut:
    """Mutual followers who accept invites and share no block with the actor."""
    back = aliased(ProfileLike)
    users = list(
        (
            await session.scalars(
                select(User)
                .join(ProfileLike, ProfileLike.liked_user_id == User.id)
                .join(
                    back,
                    and_(back.liker_id == User.id, back.liked_user_id == actor_id),
                )
                .where(
                    ProfileLike.liker_id == actor_id,
                    User.invite_policy == "mutual",
                    ~_blocked_between(actor_id, User.id),
                )
                .order_by(User.display_name, User.id)
            )
        ).all()
    )
    return await _user_list(session, users, liked_by_me=True)


async def _user_list(
    session: AsyncSession, users: list[User], *, liked_by_me: bool = False
) -> PublicUserListOut:
    ids = [user.id for user in users]
    avatars = await media_service.resolve_urls(
        session, entity_type="user", entity_ids=ids, role="avatar"
    )
    covers = await media_service.resolve_urls(
        session, entity_type="user", entity_ids=ids, role="cover"
    )
    items = [
        await public_service.public_user_row(
            session,
            user,
            avatar_url=avatars.get(user.id),
            cover_url=covers.get(user.id),
            liked_by_me=liked_by_me,
        )
        for user in users
    ]
    return PublicUserListOut(items=items, total=len(items))
