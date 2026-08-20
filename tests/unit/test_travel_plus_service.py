"""Unit coverage for Travel+ subscription lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.subscriptions.application import service as travel_plus

DATABASE_URL = "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism"


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _user(session: AsyncSession) -> User:
    user = await session.scalar(select(User).limit(1))
    if user is None:
        pytest.skip("No users in local DB for unit coverage")
    return user


@pytest.mark.asyncio
async def test_activate_rejects_bad_plan(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(AppError) as exc:
        await travel_plus.activate_travel_plus(
            session,
            user_id=user.id,
            plan="lifetime",
            source="mock_checkout",
            commit=False,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_activate_rejects_bad_source(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(AppError) as exc:
        await travel_plus.activate_travel_plus(
            session,
            user_id=user.id,
            plan="monthly",
            source="store",
            commit=False,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_activate_cancel_and_refresh_expiry(session: AsyncSession) -> None:
    user = await _user(session)
    activated = await travel_plus.activate_travel_plus(
        session,
        user_id=user.id,
        plan="yearly",
        source="admin",
        commit=True,
    )
    assert activated.travel_plus_active is True
    assert activated.travel_plus_plan == "yearly"
    assert activated.travel_plus_expires_at is not None

    canceled = await travel_plus.cancel_travel_plus(session, user_id=user.id, commit=True)
    assert canceled.travel_plus_active is False
    assert canceled.travel_plus_plan is None

    await travel_plus.activate_travel_plus(
        session,
        user_id=user.id,
        plan="monthly",
        source="mock_checkout",
        commit=True,
    )
    await session.refresh(user)
    user.travel_plus_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()
    await session.refresh(user)

    refreshed = await travel_plus.refresh_user_travel_plus(session, user)
    assert refreshed.travel_plus_active is False
    assert refreshed.travel_plus_plan is None


@pytest.mark.asyncio
async def test_activate_unknown_user(session: AsyncSession) -> None:
    with pytest.raises(AppError) as exc:
        await travel_plus.activate_travel_plus(
            session,
            user_id=uuid4(),
            plan="monthly",
            source="admin",
            commit=False,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_cancel_unknown_user(session: AsyncSession) -> None:
    with pytest.raises(AppError) as exc:
        await travel_plus.cancel_travel_plus(
            session,
            user_id=uuid4(),
            commit=False,
        )
    assert exc.value.status_code == 401
