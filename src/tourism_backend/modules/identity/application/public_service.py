"""Public user profile reads — no PII beyond display name and media."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.application.public_schemas import PublicUserOut
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.routes.application import service as routes_service
from tourism_backend.modules.routes.application.schemas import RouteListOut


async def get_public_user(session: AsyncSession, user_id: UUID) -> PublicUserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)

    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[user.id],
        role="avatar",
    )
    covers = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[user.id],
        role="cover",
    )
    return PublicUserOut(
        id=str(user.id),
        display_name=user.display_name,
        avatar_url=avatars.get(user.id),
        cover_url=covers.get(user.id),
    )


async def list_public_user_routes(
    session: AsyncSession,
    user_id: UUID,
    *,
    limit: int,
    offset: int,
) -> RouteListOut:
    # Ensure user exists (404) before listing routes.
    exists = await session.scalar(select(User.id).where(User.id == user_id))
    if exists is None:
        raise AppError(code="user_not_found", message="User not found", status_code=404)
    return await routes_service.list_public_routes_for_owner(
        session,
        owner_user_id=user_id,
        limit=limit,
        offset=offset,
    )
