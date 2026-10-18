"""A route's cost and price level (BACKEND-56, spec 33)."""

from uuid import uuid4

import pytest

from tourism_backend.modules.routes.application.route_cost import (
    KEY_BUS_FARE,
    KEY_FUEL_PER_100KM,
    KEY_LEVEL_BOUNDS,
    KEY_NIGHT_PRICES,
    CostRules,
    LegPrice,
    StopPrice,
    compute_cost,
    cost_out,
    level_for,
    parse_rules,
    validate_setting,
)

RULES = CostRules()


def _paid(low: int, high: int | None = None) -> StopPrice:
    return StopPrice(place_id=uuid4(), counts=True, min_amount=low, max_amount=high, free=False)


def _free() -> StopPrice:
    return StopPrice(place_id=uuid4(), counts=True, min_amount=None, max_amount=None, free=True)


def _unknown() -> StopPrice:
    return StopPrice(place_id=uuid4(), counts=False, min_amount=None, max_amount=None, free=False)


def test_level_bounds() -> None:
    bounds = (1500, 4000, 10000)
    assert [level_for(x, bounds) for x in (0, 1500, 1501, 4000, 9999, 10001)] == [
        1,
        1,
        2,
        2,
        3,
        4,
    ]


def test_known_cost_counts_tickets_and_fares_per_day() -> None:
    cost = compute_cost(
        stops=[_paid(1000, 1500), _free(), _paid(800)],
        legs=[
            LegPrice("walk", None, 2000),
            LegPrice("bus", None, 15000),
            LegPrice("cable_car", 900, 3000),
        ],
        day_count=1,
        rules=RULES,
    )
    assert cost["known"] is True
    assert cost["tickets"] == {"min": 1800, "max": 2300, "places": 2}
    assert cost["transport"] == {"amount": 60 + 900, "fares": 1}
    assert cost["per_person"] == {"min": 2760, "max": 3260}
    assert cost["level"] == 2
    assert cost["free"] is False
    assert cost["fuel"] is None
    assert cost["nights"] == 0
    assert cost["night_prices"] is None


def test_unknown_stop_or_ticket_leg_leaves_no_level() -> None:
    by_stop = compute_cost(stops=[_paid(500), _unknown()], legs=[], day_count=1, rules=RULES)
    assert by_stop["known"] is False
    assert by_stop["level"] is None
    assert by_stop["missing_places"] == 1
    assert by_stop["per_person"]["min"] == 500  # «от 500 ₽»
    by_leg = compute_cost(
        stops=[_free()], legs=[LegPrice("ferry", None, 4000)], day_count=1, rules=RULES
    )
    assert by_leg["known"] is False
    assert by_leg["missing_legs"] == 1


def test_fuel_and_nights_stay_outside_the_level() -> None:
    cost = compute_cost(
        stops=[_free(), _free()],
        legs=[LegPrice("car", None, 150_000), LegPrice("car", None, 50_000)],
        day_count=3,
        rules=RULES,
    )
    assert cost["free"] is True
    assert cost["level"] == 1
    assert cost["fuel"] == {"km": 200, "liters": 16.0, "amount": 1040}
    assert cost["nights"] == 2
    assert cost["night_prices"] == list(RULES.night_prices)
    assert cost["per_day"] == {"min": 0, "max": 0}


def test_a_place_twice_is_paid_once_and_days_split_the_spending() -> None:
    museum = _paid(3000)
    cost = compute_cost(stops=[museum, museum, _paid(5000)], legs=[], day_count=2, rules=RULES)
    assert cost["per_person"]["min"] == 8000
    assert cost["per_day"]["min"] == 4000
    assert cost["level"] == 2


def test_settings_are_checked() -> None:
    assert validate_setting(KEY_LEVEL_BOUNDS, " 1000, 3000 ,9000") == "1000,3000,9000"
    assert validate_setting(KEY_NIGHT_PRICES, "2000,2000,5000,9000") == "2000,2000,5000,9000"
    assert validate_setting(KEY_FUEL_PER_100KM, "7,5") == "7.5"
    for key, raw in (
        (KEY_LEVEL_BOUNDS, "3000,1000,9000"),
        (KEY_LEVEL_BOUNDS, "0,1000,9000"),
        (KEY_LEVEL_BOUNDS, "1000,2000"),
        (KEY_NIGHT_PRICES, "5000,3000,6000,9000"),
        (KEY_FUEL_PER_100KM, "100"),
        (KEY_BUS_FARE, "0"),
        (KEY_BUS_FARE, "сто"),
    ):
        with pytest.raises(ValueError, match=r"\S"):
            validate_setting(key, raw)


def test_a_bad_stored_value_falls_back_to_the_default() -> None:
    rules = parse_rules({KEY_LEVEL_BOUNDS: "9,8,7", KEY_BUS_FARE: "80"})
    assert rules.level_bounds == CostRules().level_bounds
    assert rules.bus_fare == 80


def test_cost_out() -> None:
    assert cost_out(None) is None
    cost = compute_cost(stops=[_paid(500), _unknown()], legs=[], day_count=1, rules=RULES)
    out = cost_out(cost)
    assert out is not None
    assert (out.known, out.per_person_min, out.missing_places, out.level) == (False, 500, 1, None)
