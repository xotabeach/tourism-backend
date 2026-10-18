"""Other people's companion characters, for their avatars (BACKEND-76).

Who has no picture of their own is shown as their character's portrait in
the app (FRONTEND-97), so every place that gives out someone's avatar also
gives out their character and hat, as others see them on the profile: a
hat no longer open to the person is left out. None means the one the
account was given, which the app takes from the user id.
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.identity.application.bean_hats import effective_hat, effective_kind
from tourism_backend.modules.identity.infrastructure.models import User

#: (bean_kind, bean_hat) of one person.
BeanLook = tuple[str | None, str | None]


async def bean_looks(
    session: AsyncSession, user_ids: Iterable[UUID | None]
) -> dict[UUID, BeanLook]:
    ids = {user_id for user_id in user_ids if user_id is not None}
    if not ids:
        return {}
    users = (await session.scalars(select(User).where(User.id.in_(ids)))).all()
    # Bought hats are not counted, as on the profile: nothing can be bought yet.
    return {user.id: (effective_kind(user), effective_hat(user, set())) for user in users}
