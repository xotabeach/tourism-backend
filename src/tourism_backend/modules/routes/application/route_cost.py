"""What a route costs a person, and its price level (BACKEND-56, spec 33).

The level counts tickets and paid legs only (D3, D4): the required stops'
tickets, bus and trolleybus rides (a constant from the admin when the editor
gave no price), and the cable car, ferry and train at the editor's price.
Fuel and nights are separate lines outside the level: fuel is for the whole
car, not split between people (D12), and a night's price depends on the
level the person picked, so the route keeps only the number of nights.

The cost is known only when every required stop has a price an editor
checked and no paid leg lacks one (D9, D14). Otherwise the route shows «от
X ₽, по N точкам цена не указана» and has no level, so the budget leaves it
alone in matching.

The rules live in the admin (``rt_cost_`` runtime settings), bounded so a
typo cannot price every route at zero. The result is stored on the route
(``routes.cost``, ``routes.price_level``) and recomputed when the route is
rebuilt, when a place's price changes and when the rules change.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.places.application.pricing import price_counts
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.application.schemas import RouteCostOut
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteDay,
    RouteSegment,
    RouteStop,
)
from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

_logger = logging.getLogger("tourism_backend.routes")

#: Bumped when the formula changes, so a stored cost says how it was made.
COST_FORMULA_VERSION = 1

LEVEL_NAMES = ("Экономно", "Средне", "Комфорт", "Без ограничений")

#: Legs one rides at a fixed fare when the editor gave none.
FARE_MODES = frozenset({"bus", "trolleybus"})
#: Legs whose price only an editor knows (D14).
TICKET_MODES = frozenset({"train", "cable_car", "ferry"})

KEY_PREFIX = "rt_cost_"
KEY_LEVEL_BOUNDS = "rt_cost_level_bounds"
KEY_BUS_FARE = "rt_cost_bus_fare"
KEY_FUEL_PER_100KM = "rt_cost_fuel_l_per_100km"
KEY_FUEL_PRICE = "rt_cost_fuel_price"
KEY_NIGHT_PRICES = "rt_cost_night_prices"
ALL_KEYS = frozenset(
    {KEY_LEVEL_BOUNDS, KEY_BUS_FARE, KEY_FUEL_PER_100KM, KEY_FUEL_PRICE, KEY_NIGHT_PRICES}
)


@dataclass(frozen=True, slots=True)
class CostRules:
    #: Upper bounds of «Экономно», «Средне» and «Комфорт», ₽ per person a day.
    level_bounds: tuple[int, int, int] = (1500, 4000, 10000)
    bus_fare: int = 60
    fuel_l_per_100km: float = 8.0
    fuel_price: int = 65
    #: A night for one person at each level, not falling with the level.
    night_prices: tuple[int, int, int, int] = (2000, 3500, 6000, 12000)


def _int_list(raw: str, count: int) -> list[int]:
    parts = [part.strip().replace(" ", "") for part in raw.replace(";", ",").split(",")]
    if len(parts) != count or not all(part.isdigit() for part in parts):
        raise ValueError(f"Нужно {count} целых числа через запятую.")
    return [int(part) for part in parts]


def validate_setting(key: str, value: str) -> str:
    """An admin-entered value normalised, or ``ValueError`` with the reason."""

    text = value.strip()
    if key == KEY_LEVEL_BOUNDS:
        bounds = _int_list(text, 3)
        if bounds[0] <= 0 or not bounds[0] < bounds[1] < bounds[2]:
            raise ValueError("Границы больше нуля и строго растут.")
        if bounds[2] > 1_000_000:
            raise ValueError("Не больше 1 000 000 ₽.")
        return ",".join(map(str, bounds))
    if key == KEY_NIGHT_PRICES:
        nights = _int_list(text, 4)
        if nights[0] <= 0 or any(b < a for a, b in zip(nights, nights[1:], strict=False)):
            raise ValueError("Цены больше нуля и не убывают с уровнем.")
        if nights[3] > 1_000_000:
            raise ValueError("Не больше 1 000 000 ₽.")
        return ",".join(map(str, nights))
    if key == KEY_FUEL_PER_100KM:
        try:
            liters = float(text.replace(",", "."))
        except ValueError as exc:
            raise ValueError("Нужно число, например 8 или 7,5.") from exc
        if not 3 <= liters <= 30:
            raise ValueError("Допустимо от 3 до 30 литров.")
        return f"{liters:g}"
    limits = {KEY_BUS_FARE: (10, 1000), KEY_FUEL_PRICE: (20, 500)}
    low, high = limits[key]
    if not text.isdigit():
        raise ValueError("Нужно целое число рублей.")
    number = int(text)
    if not low <= number <= high:
        raise ValueError(f"Допустимо от {low} до {high}.")
    return str(number)


def parse_rules(raw: Mapping[str, str]) -> CostRules:
    defaults = CostRules()
    values: dict[str, str] = {}
    for key in ALL_KEYS:
        stored = raw.get(key)
        if stored is None:
            continue
        try:
            values[key] = validate_setting(key, stored)
        except ValueError:
            _logger.warning("cost_setting_invalid key=%s value=%r", key, stored)
    bounds = values.get(KEY_LEVEL_BOUNDS)
    nights = values.get(KEY_NIGHT_PRICES)
    return CostRules(
        level_bounds=(
            tuple(int(x) for x in bounds.split(","))  # type: ignore[arg-type]
            if bounds
            else defaults.level_bounds
        ),
        bus_fare=int(values.get(KEY_BUS_FARE, defaults.bus_fare)),
        fuel_l_per_100km=float(values.get(KEY_FUEL_PER_100KM, defaults.fuel_l_per_100km)),
        fuel_price=int(values.get(KEY_FUEL_PRICE, defaults.fuel_price)),
        night_prices=(
            tuple(int(x) for x in nights.split(","))  # type: ignore[arg-type]
            if nights
            else defaults.night_prices
        ),
    )


async def load_rules(session: AsyncSession) -> CostRules:
    rows = (
        await session.execute(
            select(RuntimeSetting.key, RuntimeSetting.value).where(
                RuntimeSetting.key.like(f"{KEY_PREFIX}%")
            )
        )
    ).tuples()
    return parse_rules(dict(rows.all()))


def level_for(per_day: int, bounds: Sequence[int]) -> int:
    """1 «Экономно» … 4 «Без ограничений» for a day's spending per person."""

    for index, bound in enumerate(bounds):
        if per_day <= bound:
            return index + 1
    return len(bounds) + 1


@dataclass(frozen=True, slots=True)
class StopPrice:
    place_id: UUID
    counts: bool
    min_amount: int | None
    max_amount: int | None
    free: bool


@dataclass(frozen=True, slots=True)
class LegPrice:
    mode: str
    price_amount: int | None
    distance_meters: int | None


def compute_cost(
    *,
    stops: Sequence[StopPrice],
    legs: Sequence[LegPrice],
    day_count: int,
    rules: CostRules,
) -> dict[str, Any]:
    """What ``routes.cost`` keeps. Pure, for tests.

    A place visited twice is paid once. Amounts are per person; fuel is per
    car. ``per_day`` is the route's spending split over its days, the level
    is taken from its lower end.
    """

    days = max(1, day_count)
    seen: set[UUID] = set()
    tickets_min = tickets_max = 0
    missing_places = 0
    paid_places = 0
    for stop in stops:
        if stop.place_id in seen:
            continue
        seen.add(stop.place_id)
        if not stop.counts:
            missing_places += 1
            continue
        if stop.free:
            continue
        paid_places += 1
        low = stop.min_amount or 0
        tickets_min += low
        tickets_max += stop.max_amount if stop.max_amount is not None else low

    transport = 0
    fares = 0
    missing_legs = 0
    car_meters = 0
    for leg in legs:
        if leg.mode == "car":
            car_meters += leg.distance_meters or 0
        if leg.mode in FARE_MODES:
            fares += 1
            transport += leg.price_amount if leg.price_amount is not None else rules.bus_fare
        elif leg.mode in TICKET_MODES:
            if leg.price_amount is None:
                missing_legs += 1
            else:
                transport += leg.price_amount

    known = missing_places == 0 and missing_legs == 0
    total_min = tickets_min + transport
    total_max = tickets_max + transport
    per_day_min = round(total_min / days)
    per_day_max = round(total_max / days)
    fuel = None
    if car_meters:
        km = car_meters / 1000
        liters = km * rules.fuel_l_per_100km / 100
        fuel = {
            "km": round(km),
            "liters": round(liters, 1),
            "amount": round(liters * rules.fuel_price),
        }
    return {
        "version": COST_FORMULA_VERSION,
        "known": known,
        "free": known and total_max == 0,
        "level": level_for(per_day_min, rules.level_bounds) if known else None,
        "per_person": {"min": total_min, "max": total_max},
        "per_day": {"min": per_day_min, "max": per_day_max},
        "tickets": {"min": tickets_min, "max": tickets_max, "places": paid_places},
        "transport": {"amount": transport, "fares": fares},
        "missing_places": missing_places,
        "missing_legs": missing_legs,
        "fuel": fuel,
        "days": days,
        "nights": days - 1,
        "night_prices": list(rules.night_prices) if days > 1 else None,
        "level_bounds": list(rules.level_bounds),
    }


async def route_cost(session: AsyncSession, route_id: UUID, rules: CostRules) -> dict[str, Any]:
    stop_rows = (
        await session.execute(
            select(
                Place.id,
                Place.payment_status,
                Place.price_source,
                Place.price_min_amount,
                Place.price_max_amount,
                Place.price_currency,
            )
            .join(RouteStop, RouteStop.place_id == Place.id)
            .where(RouteStop.route_id == route_id, RouteStop.is_optional.is_(False))
            .order_by(RouteStop.position)
        )
    ).all()
    stops = [
        StopPrice(
            place_id=row.id,
            counts=price_counts(
                payment_status=row.payment_status,
                price_source=row.price_source,
                price_min_amount=row.price_min_amount,
                price_currency=row.price_currency,
            ),
            min_amount=row.price_min_amount,
            max_amount=row.price_max_amount,
            free=row.payment_status == "free",
        )
        for row in stop_rows
    ]
    legs = [
        LegPrice(mode=row.mode, price_amount=row.price_amount, distance_meters=row.distance_meters)
        for row in (
            await session.execute(
                select(
                    RouteSegment.mode, RouteSegment.price_amount, RouteSegment.distance_meters
                ).where(RouteSegment.route_id == route_id)
            )
        ).all()
    ]
    day_count = await session.scalar(
        select(func.count()).select_from(RouteDay).where(RouteDay.route_id == route_id)
    )
    return compute_cost(stops=stops, legs=legs, day_count=day_count or 1, rules=rules)


async def refresh_route_cost(
    session: AsyncSession, route: Route, rules: CostRules | None = None
) -> None:
    """Recompute and store the route's cost; writes only when it changed."""

    cost = await route_cost(session, route.id, rules or await load_rules(session))
    if route.cost != cost:
        route.cost = cost
    if route.price_level != cost["level"]:
        route.price_level = cost["level"]


async def refresh_costs(
    session: AsyncSession,
    *,
    place_id: UUID | None = None,
    rules: CostRules | None = None,
) -> int:
    """Recompute the routes that use [place_id], or all of them; returns how many."""

    rules = rules or await load_rules(session)
    stmt = select(Route).where(Route.publication_status != "deleted")
    if place_id is not None:
        stmt = stmt.where(
            Route.id.in_(select(RouteStop.route_id).where(RouteStop.place_id == place_id))
        )
    routes = list((await session.scalars(stmt)).all())
    for route in routes:
        await refresh_route_cost(session, route, rules)
    return len(routes)


def cost_out(cost: dict[str, Any] | None) -> RouteCostOut | None:
    """The API's view of ``routes.cost``; None before the first count."""

    if not isinstance(cost, dict) or cost.get("version") is None:
        return None
    fuel = cost.get("fuel") or {}
    return RouteCostOut(
        known=bool(cost.get("known")),
        free=bool(cost.get("free")),
        level=cost.get("level"),
        per_person_min=cost["per_person"]["min"],
        per_person_max=cost["per_person"]["max"],
        per_day_min=cost["per_day"]["min"],
        per_day_max=cost["per_day"]["max"],
        tickets_min=cost["tickets"]["min"],
        tickets_max=cost["tickets"]["max"],
        transport_amount=cost["transport"]["amount"],
        missing_places=cost.get("missing_places", 0),
        missing_legs=cost.get("missing_legs", 0),
        fuel_km=fuel.get("km"),
        fuel_amount=fuel.get("amount"),
        nights=cost.get("nights", 0),
        night_prices=cost.get("night_prices"),
    )
