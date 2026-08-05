from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.notifications.application.device_token_schemas import (
    DeviceTokenIn,
)
from tourism_backend.modules.notifications.infrastructure.models import DeviceToken


async def upsert_device_token(
    session: AsyncSession,
    *,
    user_id: UUID,
    payload: DeviceTokenIn,
) -> None:
    now = datetime.now(UTC)
    existing = await session.scalar(select(DeviceToken).where(DeviceToken.token == payload.token))
    if existing is None:
        session.add(
            DeviceToken(
                id=uuid4(),
                user_id=user_id,
                token=payload.token,
                platform=payload.platform,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        existing.user_id = user_id
        existing.platform = payload.platform
        existing.updated_at = now
    await session.commit()


async def delete_device_token(
    session: AsyncSession,
    *,
    user_id: UUID,
    token: str,
) -> None:
    row = await session.scalar(
        select(DeviceToken).where(
            DeviceToken.token == token,
            DeviceToken.user_id == user_id,
        )
    )
    if row is not None:
        await session.delete(row)
        await session.commit()


async def list_tokens_for_user(
    session: AsyncSession,
    *,
    user_id: UUID,
) -> list[DeviceToken]:
    return list(
        (await session.scalars(select(DeviceToken).where(DeviceToken.user_id == user_id))).all()
    )
