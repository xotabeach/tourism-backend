"""A companion's character: build, hat and clothes (BACKEND-65, BACKEND-71).

The build is one of the four characters the designer drew, all free
(owner, 2026-10-15). Hats and clothes come in tiers: some free, some open
once a person has earned enough travel points (the points are not spent:
spending them would lower the rank and the place in the top), the rest with
Travel+ or a purchase. An expert has every hat and every piece of clothing
(owner, 2026-10-15). Payment is not connected yet; a bought hat is a row in
``user_bean_hats``.
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
    BeanHat("headphones", "Наушники", "points", 500),
)
BY_SLUG = {hat.slug: hat for hat in CATALOG}

#: The four builds, in the order on screen; all free.
KINDS: tuple[tuple[str, str], ...] = (
    ("pin", "Синий"),
    ("tall", "Зелёный"),
    ("small", "Жёлтый"),
    ("round", "Красный"),
)
KIND_SLUGS = {slug for slug, _ in KINDS}

#: Clothes, priced like hats (owner, 2026-10-15: tiers as proposed).
OUTFITS: tuple[BeanHat, ...] = (
    BeanHat("hoodie", "Худи", "free"),
    BeanHat("sweater", "Свитер в полоску", "free"),
    BeanHat("varsity", "Бомбер", "points", 1500),
    BeanHat("vest", "Жилет с сумкой", "premium"),
)
OUTFIT_BY_SLUG = {outfit.slug: outfit for outfit in OUTFITS}


class BeanHatOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str
    tier: HatTier
    points_required: int = 0
    available: bool
    #: Why it is open: «free», «points», «travel_plus», «purchase».
    unlocked_by: str | None = None


class BeanKindOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str


class BeanHatsOut(BaseModel):
    """Everything the «Мой персонаж» screen shows: hats in ``items`` (the
    name older builds read), the builds and the clothes."""

    model_config = ConfigDict(extra="forbid")

    selected: str | None
    travel_points: int
    travel_plus_active: bool
    is_expert: bool = False
    #: Buying for money is not connected yet; the app shows «скоро».
    purchase_available: bool = False
    items: list[BeanHatOut]
    #: The chosen build; None is the one the account was given.
    kind: str | None = None
    kinds: list[BeanKindOut] = []
    #: The chosen clothes; None is none.
    outfit: str | None = None
    outfits: list[BeanHatOut] = []


class BeanLook(BaseModel):
    """What others see of a person's character."""

    hat: str | None = None
    kind: str | None = None
    outfit: str | None = None


class BeanHatSelectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: None goes back to the hat the account was given (the build the
    #: account was given, no clothes).
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
    # The expert rank is the top one: everything is open (owner, 2026-10-15).
    if user.is_expert:
        return "expert"
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


def effective_outfit(user: User) -> str | None:
    """The clothes others see, while still open to the person."""
    outfit = OUTFIT_BY_SLUG.get(user.bean_outfit or "")
    if outfit is None or _unlocked_by(outfit, user, set()) is None:
        return None
    return outfit.slug


def effective_kind(user: User) -> str | None:
    return user.bean_kind if user.bean_kind in KIND_SLUGS else None


async def effective_looks(session: AsyncSession, users: list[User]) -> dict[UUID, BeanLook]:
    """What others see of each person's character (group screens)."""
    hats = await effective_hats(session, users)
    return {
        user.id: BeanLook(
            hat=hats.get(user.id),
            kind=effective_kind(user),
            outfit=effective_outfit(user),
        )
        for user in users
    }


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
        is_expert=user.is_expert,
        kind=effective_kind(user),
        kinds=[BeanKindOut(slug=slug, title=title) for slug, title in KINDS],
        outfit=effective_outfit(user),
        outfits=[
            BeanHatOut(
                slug=outfit.slug,
                title=outfit.title,
                tier=outfit.tier,
                points_required=outfit.points_required,
                available=(reason := _unlocked_by(outfit, user, set())) is not None,
                unlocked_by=reason,
            )
            for outfit in OUTFITS
        ],
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


async def select_kind(
    session: AsyncSession, *, user_id: UUID, payload: BeanHatSelectIn
) -> BeanHatsOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    if payload.slug is not None and payload.slug not in KIND_SLUGS:
        raise AppError(code="bean_kind_unknown", message="Такого персонажа нет", status_code=404)
    user.bean_kind = payload.slug
    user.updated_at = datetime.now(UTC)
    await session.commit()
    return await list_hats(session, user_id=user_id)


async def select_outfit(
    session: AsyncSession, *, user_id: UUID, payload: BeanHatSelectIn
) -> BeanHatsOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(code="unauthorized", message="Authentication required", status_code=401)
    if payload.slug is not None:
        outfit = OUTFIT_BY_SLUG.get(payload.slug)
        if outfit is None:
            raise AppError(code="bean_outfit_unknown", message="Такой одежды нет", status_code=404)
        if _unlocked_by(outfit, user, set()) is None:
            raise AppError(
                code="bean_outfit_locked",
                message=(
                    f"Откроется на {outfit.points_required} ТП"
                    if outfit.tier == "points"
                    else "Доступно с Travel+ или после покупки"
                ),
                status_code=409,
            )
    user.bean_outfit = payload.slug
    user.updated_at = datetime.now(UTC)
    await session.commit()
    return await list_hats(session, user_id=user_id)
