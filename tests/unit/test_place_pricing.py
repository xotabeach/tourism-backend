"""A place's price as the editors keep it (BACKEND-55, spec 33 D15)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from tourism_backend.config import Settings
from tourism_backend.modules.admin.presentation.place_prices_admin import (
    PlacePricesAdmin,
    hint_text,
)
from tourism_backend.modules.admin.presentation.views import register_views
from tourism_backend.modules.places.application.pricing import (
    PriceInputError,
    apply_price,
    parse_price_input,
    price_counts,
    price_stale,
)
from tourism_backend.modules.routes.application.structure_rules import PlannedSegment

NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _parse(status: str, low: str = "", high: str = "", notes: str = "") -> object:
    return parse_price_input(payment_status=status, min_amount=low, max_amount=high, notes=notes)


def test_price_input_rules() -> None:
    paid = parse_price_input(
        payment_status="paid", min_amount=" 1 200 ", max_amount="1500", notes=" льготы "
    )
    assert (paid.min_amount, paid.max_amount, paid.notes) == (1200, 1500, "льготы")
    # One price is written as «от»; «до» equal to it adds nothing.
    assert (
        parse_price_input(
            payment_status="paid", min_amount="300", max_amount="300", notes=None
        ).max_amount
        is None
    )
    # Paid without a price is allowed: the status is known, the cost is not.
    assert (
        parse_price_input(
            payment_status="paid", min_amount="", max_amount="", notes=None
        ).min_amount
        is None
    )
    for status, low, high in (
        ("maybe", "", ""),
        ("free", "100", ""),
        ("unknown", "100", ""),
        ("paid", "сто", ""),
        ("paid", "-5", ""),
        ("paid", "", "500"),
        ("paid", "500", "300"),
        ("paid", "1000000", ""),
    ):
        with pytest.raises(PriceInputError):
            parse_price_input(payment_status=status, min_amount=low, max_amount=high, notes=None)


def test_apply_price_marks_editor_and_unknown_takes_it_back() -> None:
    place = SimpleNamespace(price_source="import", price_checked_at=None)
    apply_price(
        place,
        parse_price_input(  # type: ignore[arg-type]
            payment_status="paid", min_amount="500", max_amount="", notes=""
        ),
        now=NOW,
    )
    assert place.price_source == "editor"
    assert place.price_checked_at == NOW
    assert place.is_paid is True
    assert place.price_currency == "RUB"
    apply_price(
        place,
        parse_price_input(  # type: ignore[arg-type]
            payment_status="unknown", min_amount="", max_amount="", notes=""
        ),
        now=NOW,
    )
    assert place.price_source is None
    assert place.price_checked_at is None
    assert place.price_min_amount is None


def test_only_editor_prices_count() -> None:
    def counts(status: str, source: str | None, low: int | None, cur: str = "RUB") -> bool:
        return price_counts(
            payment_status=status, price_source=source, price_min_amount=low, price_currency=cur
        )

    assert counts("free", "editor", None)
    assert counts("paid", "editor", 400)
    assert not counts("free", "import", None)
    assert not counts("paid", "import", 400)
    assert not counts("paid", "editor", None)
    assert not counts("paid", "editor", 5, "EUR")
    assert not counts("unknown", "editor", None)


def test_price_goes_stale_after_a_year() -> None:
    assert not price_stale(None, now=NOW)
    assert not price_stale(NOW - timedelta(days=300), now=NOW)
    assert price_stale(NOW - timedelta(days=400), now=NOW)


def test_hint_text() -> None:
    assert hint_text(None) == ""
    assert hint_text({"source": "osm", "min": 300, "max": 500, "currency": "RUB"}) == (
        "OSM: 300–500 RUB"
    )
    assert hint_text({"source": "seed", "notes": "вход по билетам"}) == "Сид: (вход по билетам)"


def test_segment_price_is_part_of_the_plan() -> None:
    a, b = uuid4(), uuid4()
    base = PlannedSegment(
        leg_index=0,
        seq=0,
        from_stop_id=a,
        to_stop_id=b,
        mode="cable_car",
        role="main",
        origin="editor",
        distance_meters=1000,
        duration_seconds=600,
    )
    assert base.price_amount is None
    # A changed price is a change the rebuild writes.
    assert base != PlannedSegment(**{**_fields(base), "price_amount": 900})


def _fields(segment: PlannedSegment) -> dict[str, object]:
    return {name: getattr(segment, name) for name in PlannedSegment.__slots__}


def test_prices_page_is_registered_gated_and_parses() -> None:
    class _FakeAdmin:
        def __init__(self) -> None:
            self.views: list[object] = []

        def add_view(self, view: object) -> None:
            self.views.append(view)

    admin = _FakeAdmin()
    register_views(admin, Settings(app_env="test"))
    assert PlacePricesAdmin in admin.views

    def _request(permissions: set[str]) -> Request:
        request = Request({"type": "http", "session": {}})  # type: ignore[arg-type]
        request.state.admin_permissions = frozenset(permissions)
        return request

    view = PlacePricesAdmin()
    assert view.is_accessible(_request({"places.read"})) is True
    assert view.is_accessible(_request(set())) is False

    templates = (
        Path(__file__).resolve().parents[2] / "src/tourism_backend/modules/admin/theme/templates"
    )
    env = Environment(loader=FileSystemLoader(str(templates)), autoescape=True)
    for name in ("place_prices.html", "route_structure.html"):
        env.parse((templates / "sqladmin" / name).read_text(encoding="utf-8"))
