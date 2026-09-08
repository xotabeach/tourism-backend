"""Preferences affect both individual POI choice and honest plan warnings."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.route_builder.application import place_picker as picker
from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn
from tourism_backend.modules.route_builder.application.session_service import (
    _recent_proposal_place_ids,
)


def _place(**overrides: object) -> Place:
    return Place(
        **{
            "id": uuid4(),
            "name": "Парк",
            "is_paid": False,
            "payment_status": "unknown",
            "typical_crowding": "unknown",
            "price_currency": "RUB",
            **overrides,
        }
    )


def test_crowds_change_poi_rank_only_when_requested() -> None:
    low, high, unknown = [_place(typical_crowding=value) for value in ("low", "high", "unknown")]
    plain = RouteMatchParamsIn(city="Ялта")
    quiet = plain.model_copy(update={"avoid_crowds": True})
    assert picker._score_place(plain, low, "") == picker._score_place(plain, high, "")
    assert (
        picker._score_place(quiet, low, "")
        > picker._score_place(quiet, unknown, "")
        > picker._score_place(quiet, high, "")
    )


def test_budget_prefers_known_affordable_prices_not_unknown_as_free() -> None:
    params = RouteMatchParamsIn(city="Ялта", budget_amount=2000)
    cheap = _place(price_min_amount=100)
    expensive = _place(price_min_amount=1800)
    unknown = _place()
    foreign = _place(price_min_amount=1, price_currency="USD")
    free = _place(payment_status="free")
    assert (
        picker._score_place(params, free, "")
        > picker._score_place(params, cheap, "")
        > picker._score_place(params, expensive, "")
        > picker._score_place(params, unknown, "")
    )
    assert picker._score_place(params, foreign, "") == picker._score_place(params, unknown, "")


@pytest.mark.parametrize(
    ("filters", "paid", "status", "price", "currency", "allowed"),
    [
        ({"paid_ok": False}, True, "unknown", None, "RUB", False),
        ({"paid_ok": False}, False, "paid", None, "RUB", False),
        ({"paid_ok": False}, False, "unknown", 100, "RUB", False),
        ({"paid_ok": False}, False, "free", 0, "RUB", True),
        ({"budget_amount": 0}, True, "paid", None, "RUB", False),
        ({"budget_amount": 1000}, True, "paid", 1001, "RUB", False),
        ({"budget_amount": 1000}, True, "paid", 1000, "RUB", True),
        ({"budget_amount": 1000}, True, "paid", 1001, "USD", True),
        ({"budget_amount": 1000}, False, "unknown", None, "RUB", True),
    ],
)
def test_actual_sql_constraints_enforce_known_costs(
    filters: dict[str, object],
    paid: bool,
    status: str,
    price: int | None,
    currency: str,
    allowed: bool,
) -> None:
    # Execute the ORM predicates against a minimal in-memory table. No mocks
    # of the predicate itself and no dependency on a running PostGIS service.
    params = RouteMatchParamsIn.model_validate({"city": "Ялта", **filters})
    query = select(Place.name).where(*picker._hard_place_constraints(params))
    sql = str(query.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            "CREATE TABLE places (name TEXT, temporary_closure_status TEXT, "
            "is_paid BOOLEAN, payment_status TEXT, price_min_amount INTEGER, price_currency TEXT)"
        )
        database.execute(
            "INSERT INTO places VALUES (?, NULL, ?, ?, ?, ?)",
            ("Парк", paid, status, price, currency),
        )
        assert bool(database.execute(sql).fetchall()) is allowed


def test_unknown_prices_and_crowds_are_reported_without_technical_field_names() -> None:
    params = RouteMatchParamsIn(city="Ялта", budget_amount=1000, avoid_crowds=True)
    places = [
        picker.picked_place_from_orm(_place()),
        picker.picked_place_from_orm(_place(price_min_amount=100, price_currency="USD")),
    ]
    warnings = picker.place_planning_warnings(params, places)
    assert any("2 из 2" in warning for warning in warnings)
    assert any("не смета" in warning for warning in warnings)
    assert any("нет данных о людности" in warning for warning in warnings)
    assert not any("avoid_crowds" in warning for warning in warnings)


def test_known_free_places_do_not_get_an_unknown_price_warning() -> None:
    places = [picker.picked_place_from_orm(_place(payment_status="free"))]
    warnings = picker.place_planning_warnings(
        RouteMatchParamsIn(city="Ялта", paid_ok=False), places
    )
    assert not any("стоимость посещения в рублях" in warning for warning in warnings)


@pytest.mark.parametrize("pool_size", [2, 6])
async def test_repeat_build_prefers_fresh_places_but_small_catalogue_still_works(
    monkeypatch: pytest.MonkeyPatch,
    pool_size: int,
) -> None:
    places = [_place(name=f"Парк {i}") for i in range(pool_size)]
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
    result = MagicMock()
    result.all.return_value = places
    session.scalars = AsyncMock(side_effect=[[], result])
    monkeypatch.setattr(picker, "_categories_for_places", AsyncMock(return_value={}))
    monkeypatch.setattr(
        picker,
        "_coords_for_places",
        AsyncMock(
            return_value={place.id: (34.16 + i * 0.001, 44.49) for i, place in enumerate(places)}
        ),
    )
    monkeypatch.setattr(picker, "covers_for_places", AsyncMock(return_value={}))
    chosen = await picker.pick_places_for_params(
        session,
        params=RouteMatchParamsIn(city="Ялта", duration="d1_2"),
        max_points=3,
        recent_place_ids=frozenset(place.id for place in places[:3]),
    )
    expected = places[3:6] if pool_size == 6 else places
    assert [place.place_id for place in chosen] == [place.id for place in expected]


async def test_diversity_lookup_is_bounded_to_current_owner_and_session() -> None:
    user_id, session_id, place_id = uuid4(), uuid4(), uuid4()
    session = MagicMock(spec=AsyncSession)
    rows = MagicMock()
    rows.all.return_value = [[place_id], [place_id]]
    session.scalars = AsyncMock(return_value=rows)
    assert await _recent_proposal_place_ids(
        session, user_id=user_id, session_id=session_id
    ) == frozenset({place_id})
    query = str(session.scalars.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "route_planning_messages.session_id =" in query
    assert "route_planning_messages.user_id =" in query
    assert "route_proposals.user_id =" in query
    assert "LIMIT 3" in query
