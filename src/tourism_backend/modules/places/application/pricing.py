"""A place's price as the editors keep it (BACKEND-55, spec 33 D6/D15).

The route's cost counts only what an editor confirmed: a place marked free,
or paid with a price in roubles. What OSM or the seed said is kept as a hint
next to it. A confirmed price older than :data:`PRICE_RECHECK_AFTER` is
still counted, but goes back to the editors' list to be checked again.

The editors' list is the places of published routes that do not count yet,
the ones in most routes first: that is the order in which routes get their
cost soonest (the measurement in spec 33).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route, RouteStop

PRICE_RECHECK_AFTER = timedelta(days=365)
#: A ticket above this is a typo rather than a Crimean museum.
MAX_PRICE_AMOUNT = 100_000

PaymentStatus = Literal["free", "paid", "unknown"]


class PriceInputError(ValueError):
    """The editor's input, said in words for the admin page."""


@dataclass(frozen=True, slots=True)
class PriceInput:
    payment_status: PaymentStatus
    min_amount: int | None
    max_amount: int | None
    notes: str | None


def _amount(raw: str | None, label: str) -> int | None:
    text = (raw or "").strip().replace(" ", "").replace(" ", "")
    if not text:
        return None
    if not text.isdigit():
        raise PriceInputError(f"{label}: целое число рублей, например 500")
    value = int(text)
    if value > MAX_PRICE_AMOUNT:
        raise PriceInputError(f"{label}: больше {MAX_PRICE_AMOUNT} ₽, проверьте число")
    return value


def parse_price_input(
    *,
    payment_status: str | None,
    min_amount: str | None,
    max_amount: str | None,
    notes: str | None,
) -> PriceInput:
    """Check what the editor typed. Free clears the amounts; a price makes it paid."""

    status = (payment_status or "").strip()
    if status not in ("free", "paid", "unknown"):
        raise PriceInputError("Выберите: бесплатно, платно или не знаю")
    low = _amount(min_amount, "Цена от")
    high = _amount(max_amount, "Цена до")
    if status != "paid" and (low is not None or high is not None):
        raise PriceInputError("Цена указана, а место не отмечено платным")
    if high is not None and low is None:
        raise PriceInputError("Укажите «от»: одна цена пишется в поле «от»")
    if high is not None and low is not None and high < low:
        raise PriceInputError("«До» меньше, чем «от»")
    if high == low:
        high = None
    cleaned_notes = (notes or "").strip() or None
    return PriceInput(
        payment_status=status,  # type: ignore[arg-type]
        min_amount=low,
        max_amount=high,
        notes=cleaned_notes[:500] if cleaned_notes else None,
    )


def apply_price(place: Place, price: PriceInput, *, now: datetime) -> None:
    """The editor's price on the place, marked as checked now.

    «Не знаю» is the editor taking a status back: it is no one's check then.
    """

    place.payment_status = price.payment_status
    place.is_paid = price.payment_status == "paid"
    place.price_min_amount = price.min_amount
    place.price_max_amount = price.max_amount
    place.price_currency = "RUB"
    place.price_notes = price.notes
    if price.payment_status == "unknown":
        place.price_source = None
        place.price_checked_at = None
    else:
        place.price_source = "editor"
        place.price_checked_at = now
    place.updated_at = now


def price_counts(
    *,
    payment_status: str,
    price_source: str | None,
    price_min_amount: int | None,
    price_currency: str | None,
) -> bool:
    """Whether the place's price may go into a route's cost (D9, D15)."""

    if price_source != "editor":
        return False
    if payment_status == "free":
        return True
    return payment_status == "paid" and price_min_amount is not None and price_currency == "RUB"


def price_stale(checked_at: datetime | None, *, now: datetime) -> bool:
    return checked_at is not None and now - checked_at > PRICE_RECHECK_AFTER


@dataclass(frozen=True, slots=True)
class PriceQueueRow:
    place_id: UUID
    name: str
    routes: int
    payment_status: str
    price_source: str | None
    price_min_amount: int | None
    price_max_amount: int | None
    price_notes: str | None
    checked_at: datetime | None
    hint: dict[str, Any] | None
    counts: bool
    stale: bool


@dataclass(frozen=True, slots=True)
class PriceQueue:
    rows: list[PriceQueueRow]
    places_in_routes: int
    confirmed: int
    stale: int
    routes: int
    routes_with_cost: int


async def price_queue(
    session: AsyncSession,
    *,
    now: datetime,
    show: Literal["todo", "stale", "all"] = "todo",
    search: str | None = None,
) -> PriceQueue:
    """Places of published routes with how far their prices are from counting."""

    usage = (
        select(RouteStop.place_id, func.count(func.distinct(RouteStop.route_id)).label("routes"))
        .join(Route, Route.id == RouteStop.route_id)
        .where(Route.publication_status == "published", RouteStop.is_optional.is_(False))
        .group_by(RouteStop.place_id)
        .subquery()
    )
    stmt = select(
        Place.id,
        Place.name,
        usage.c.routes,
        Place.payment_status,
        Place.price_source,
        Place.price_min_amount,
        Place.price_max_amount,
        Place.price_notes,
        Place.price_currency,
        Place.price_checked_at,
        Place.price_hint,
    ).join(usage, usage.c.place_id == Place.id)
    all_rows = []
    for row in (await session.execute(stmt)).all():
        counts = price_counts(
            payment_status=row.payment_status,
            price_source=row.price_source,
            price_min_amount=row.price_min_amount,
            price_currency=row.price_currency,
        )
        all_rows.append(
            PriceQueueRow(
                place_id=row.id,
                name=row.name,
                routes=row.routes,
                payment_status=row.payment_status,
                price_source=row.price_source,
                price_min_amount=row.price_min_amount,
                price_max_amount=row.price_max_amount,
                price_notes=row.price_notes,
                checked_at=row.price_checked_at,
                hint=row.price_hint,
                counts=counts,
                stale=counts and price_stale(row.price_checked_at, now=now),
            )
        )

    # A route has its cost when every required stop counts.
    route_rows = (
        await session.execute(
            select(RouteStop.route_id, RouteStop.place_id)
            .join(Route, Route.id == RouteStop.route_id)
            .where(Route.publication_status == "published", RouteStop.is_optional.is_(False))
        )
    ).all()
    counted = {row.place_id for row in all_rows if row.counts}
    route_places: dict[UUID, set[UUID]] = {}
    for route_id, place_id in route_rows:
        route_places.setdefault(route_id, set()).add(place_id)

    if show == "todo":
        picked = [row for row in all_rows if not row.counts]
    elif show == "stale":
        picked = [row for row in all_rows if row.stale]
    else:
        picked = list(all_rows)
    needle = (search or "").strip().lower()
    if needle:
        picked = [row for row in picked if needle in row.name.lower()]
    picked.sort(key=lambda row: (-row.routes, row.name))
    return PriceQueue(
        rows=picked,
        places_in_routes=len(all_rows),
        confirmed=len(counted),
        stale=sum(1 for row in all_rows if row.stale),
        routes=len(route_places),
        routes_with_cost=sum(1 for places in route_places.values() if places <= counted),
    )
