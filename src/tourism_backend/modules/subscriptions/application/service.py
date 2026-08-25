"""Travel+ subscription lifecycle (no real store billing yet)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import AppEnvironment
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.subscriptions.application.entitlements import (
    mock_self_activate_allowed,
)
from tourism_backend.modules.subscriptions.infrastructure.models import (
    TravelPlusSubscription,
)

PLAN_DURATIONS = {
    "monthly": timedelta(days=30),
    "yearly": timedelta(days=365),
}


def _now() -> datetime:
    return datetime.now(UTC)


async def refresh_user_travel_plus(session: AsyncSession, user: User) -> User:
    """Expire stale denormalized flags and return the updated user."""
    now = _now()
    if (
        user.travel_plus_active
        and user.travel_plus_expires_at is not None
        and user.travel_plus_expires_at <= now
    ):
        await _expire_active_rows(session, user_id=user.id, now=now)
        user.travel_plus_active = False
        user.travel_plus_expires_at = None
        user.travel_plus_plan = None
        user.updated_at = now
        await session.commit()
        await session.refresh(user)
    return user


async def activate_travel_plus(
    session: AsyncSession,
    *,
    user_id: UUID,
    plan: str,
    source: str,
    created_by_principal_id: UUID | None = None,
    commit: bool = True,
    app_env: AppEnvironment | str | None = None,
) -> User:
    if plan not in PLAN_DURATIONS:
        raise AppError(
            code="validation_error",
            message="plan must be monthly or yearly",
            status_code=422,
        )
    if source not in {"admin", "mock_checkout"}:
        raise AppError(
            code="validation_error",
            message="unsupported subscription source",
            status_code=422,
        )
    if source == "mock_checkout" and not mock_self_activate_allowed(app_env):
        raise AppError(
            code="mock_checkout_disabled",
            message="Самоактивация Тревел+ недоступна в этом окружении",
            status_code=403,
        )

    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)

    now = _now()
    await _expire_active_rows(session, user_id=user_id, now=now)

    ends_at = now + PLAN_DURATIONS[plan]
    session.add(
        TravelPlusSubscription(
            id=uuid4(),
            user_id=user_id,
            plan=plan,
            status="active",
            starts_at=now,
            ends_at=ends_at,
            canceled_at=None,
            source=source,
            created_by_principal_id=created_by_principal_id,
            created_at=now,
            updated_at=now,
        )
    )
    user.travel_plus_active = True
    user.travel_plus_plan = plan
    user.travel_plus_expires_at = ends_at
    user.updated_at = now
    if commit:
        await session.commit()
        await session.refresh(user)
    else:
        await session.flush()
    return user


async def cancel_travel_plus(
    session: AsyncSession,
    *,
    user_id: UUID,
    created_by_principal_id: UUID | None = None,
    commit: bool = True,
) -> User:
    del created_by_principal_id  # reserved for future audit rows
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)

    now = _now()
    active = await session.scalar(
        select(TravelPlusSubscription).where(
            TravelPlusSubscription.user_id == user_id,
            TravelPlusSubscription.status == "active",
        )
    )
    if active is not None:
        active.status = "canceled"
        active.canceled_at = now
        active.updated_at = now
        if active.ends_at > now:
            active.ends_at = now

    user.travel_plus_active = False
    user.travel_plus_expires_at = None
    user.travel_plus_plan = None
    user.updated_at = now
    if commit:
        await session.commit()
        await session.refresh(user)
    else:
        await session.flush()
    return user


async def _expire_active_rows(
    session: AsyncSession,
    *,
    user_id: UUID,
    now: datetime,
) -> None:
    rows = list(
        (
            await session.scalars(
                select(TravelPlusSubscription).where(
                    TravelPlusSubscription.user_id == user_id,
                    TravelPlusSubscription.status == "active",
                )
            )
        ).all()
    )
    for row in rows:
        row.status = "expired" if row.ends_at <= now else "canceled"
        row.canceled_at = now if row.status == "canceled" else row.canceled_at
        row.updated_at = now
        if row.ends_at > now:
            row.ends_at = now
