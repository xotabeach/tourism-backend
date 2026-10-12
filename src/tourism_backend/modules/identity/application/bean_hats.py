"""Hats for a companion's character (BACKEND-65, owner's decision 2026-10-12).

A person picks only the hat; the body, markings and colour come from the
account. Four hats are free, six open once a person has earned enough
travel points (the points are not spent: spending them would lower the
rank and the place in the top), and ten come with Travel+ or a purchase.
Payment is not connected yet; a bought hat is a row in ``user_bean_hats``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.identity.infrastructure.models import User, UserBeanHat

HatTier = Literal["free", "points", "premium"]


@dataclass(frozen=True, slots=True)
class BeanHat:
    slug: str
    title: str
    tier: HatTier
    points_required: int = 0


#: The catalogue; the app draws each slug. Order is the order on screen.
CATALOG: tuple[BeanHat, ...] = (
    BeanHat("beanie", "Шапка с помпоном", "free"),
    BeanHat("cap", "Кепка", "free"),
    BeanHat("headband", "Повязка с листиком", "free"),
    BeanHat("tuft", "Хохолок", "free"),
    BeanHat("panama", "Панама", "points", 300),
    BeanHat("straw_hat", "Соломенная шляпа", "points", 800),
    BeanHat("bandana", "Бандана", "points", 1500),
    BeanHat("ushanka", "Ушанка", "points", 3000),
    BeanHat("beret", "Берет", "points", 5000),
    BeanHat("climbing_helmet", "Каска альпиниста", "points", 8000),
    BeanHat("cowboy", "Ковбойская шляпа", "premium"),
    BeanHat("crown", "Корона", "premium"),
    BeanHat("tricorn", "Треуголка", "premium"),
    BeanHat("viking", "Шлем викинга", "premium"),
    BeanHat("chef", "Колпак повара", "premium"),
    BeanHat("top_hat", "Цилиндр", "premium"),
    BeanHat("wreath", "Венок", "premium"),
    BeanHat("sailor", "Бескозырка", "premium"),
    BeanHat("wizard", "Колпак волшебника", "premium"),
    BeanHat("party", "Праздничный колпак", "premium"),
)
BY_SLUG = {hat.slug: hat for hat in CATALOG}


class BeanHatOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str
    tier: HatTier
    points_required: int = 0
    available: bool
    #: Why it is open: «free», «points», «travel_plus», «purchase».
    unlocked_by: str | None = None


class BeanHatsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected: str | None
    travel_points: int
    travel_plus_active: bool
    #: Buying for money is not connected yet; the app shows «скоро».
    purchase_available: bool = False
    items: list[BeanHatOut]


class BeanHatSelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: None goes back to the hat the account was given.
    slug: str | None


async def _bought(session: AsyncSession, user_id: UUID) -> set[str]:
    return set(
        (
            await session.scalars(
                select(UserBeanHat.hat_slug).where(UserBeanHat.user_id == user_id)
            )
        ).all()
    )


def _unlocked_by(hat: BeanHat, user: User, bought: set[str]) -> str | None:
    if hat.tier == "free":
        return "free"
    if hat.slug in bought:
        return "purchase"
    if hat.tier == "points" and user.travel_points >= hat.points_required:
        return "points"
    # Travel+ also opens every hat earned with points.
    if user.travel_plus_active:
        return "travel_plus"
    return None


def effective_hat(user: User, bought: set[str]) -> str | None:
    """The hat others see: the chosen one while it is still open to the
    person (Travel+ may have ended), else none and the app draws the hat the
    account was given."""
    slug = user.bean_hat
    hat = BY_SLUG.get(slug or "")
    if hat is None or _unlocked_by(hat, user, bought) is None:
        return None
    return hat.slug


async def effective_hats(session: AsyncSession, users: list[User]) -> dict[UUID, str | None]:
    """[effective_hat] for many people at once (group screens)."""
    chosen = [user for user in users if user.bean_hat]
    if not chosen:
        return {user.id: None for user in users}
    rows = (
        await session.execute(
            select(UserBeanHat.user_id, UserBeanHat.hat_slug).where(
                UserBeanHat.user_id.in_([user.id for user in chosen])
            )
        )
    ).all()
    bought: dict[UUID, set[str]] = {}
    for user_id, slug in rows:
        bought.setdefault(user_id, set()).add(slug)
    return {user.id: effective_hat(user, bought.get(user.id, set())) for user in users}


async def list_hats(session: AsyncSession, *, user_id: UUID) -> BeanHatsOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    bought = await _bought(session, user_id)
    return BeanHatsOut(
        selected=effective_hat(user, bought),
        travel_points=user.travel_points,
        travel_plus_active=user.travel_plus_active,
        items=[
            BeanHatOut(
                slug=hat.slug,
                title=hat.title,
                tier=hat.tier,
                points_required=hat.points_required,
                available=(reason := _unlocked_by(hat, user, bought)) is not None,
                unlocked_by=reason,
            )
            for hat in CATALOG
        ],
    )


async def select_hat(
    session: AsyncSession, *, user_id: UUID, payload: BeanHatSelectIn
) -> BeanHatsOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    if payload.slug is not None:
        hat = BY_SLUG.get(payload.slug)
        if hat is None:
            raise AppError(code="bean_hat_unknown", message="Такой шапки нет", status_code=404)
        if _unlocked_by(hat, user, await _bought(session, user_id)) is None:
            raise AppError(
                code="bean_hat_locked",
                message=(
                    f"Шапка откроется на {hat.points_required} ТП"
                    if hat.tier == "points"
                    else "Шапка доступна с Travel+ или после покупки"
                ),
                status_code=409,
            )
    user.bean_hat = payload.slug
    user.updated_at = datetime.now(UTC)
    await session.commit()
    return await list_hats(session, user_id=user_id)
