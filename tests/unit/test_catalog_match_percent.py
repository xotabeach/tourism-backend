"""Chat carousel snapshot of the match percent (spec 06, D7, D17)."""

from types import SimpleNamespace
from uuid import uuid4

from tourism_backend.modules.route_builder.application.session_service import (
    _catalog_match_block,
)


def _route(**extra: object) -> SimpleNamespace:
    base = {
        "id": uuid4(),
        "name": "Ялта · море",
        "cover_image_url": None,
        "distance_meters": 4200,
        "transport_mode": "walk",
        "suitable_for_children": True,
        "seasonality": ["лето"],
        "difficulty": "easy",
        "stops_count": 4,
        "estimated_duration_minutes": 240,
        "rating_average": 4.6,
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _hit(percent: int | None, mismatches: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        route=_route(),
        locality_label="Ялта",
        match_percent=percent,
        mismatches=mismatches or [],
    )


def test_percent_and_main_mismatch_are_stored_with_the_formula_version() -> None:
    matched = SimpleNamespace(
        hits=[_hit(75, ["старт не совпал", "другой темп"])],
        ideal=[],
        close=[],
        formula_version=2,
    )
    block = _catalog_match_block(matched)
    assert block is not None
    item = block.routes[0]
    assert item.match_percent == 75
    assert item.main_mismatch == "старт не совпал"
    assert item.formula_version == 2
    assert item.rating == 4.6


def test_no_percent_means_nothing_else_is_stored() -> None:
    matched = SimpleNamespace(
        hits=[_hit(None, ["старт не совпал"])], ideal=[], close=[], formula_version=2
    )
    item = _catalog_match_block(matched).routes[0]  # type: ignore[union-attr]
    assert item.match_percent is None
    assert item.main_mismatch is None
    assert item.formula_version is None


def test_older_shape_with_only_the_two_bands_still_builds() -> None:
    matched = SimpleNamespace(ideal=[_hit(None)], close=[_hit(None)])
    block = _catalog_match_block(matched)
    assert block is not None
    assert len(block.routes) == 2
    assert block.routes[0].match_percent is None


def test_old_stored_message_payload_without_the_new_fields_still_parses() -> None:
    from tourism_backend.modules.route_builder.application.schemas import CatalogMatchBlockOut

    old = {
        "type": "catalog_match",
        "routes": [{"route_id": "r1", "title": "Старый маршрут", "stops_count": 3}],
    }
    parsed = CatalogMatchBlockOut.model_validate(old)
    assert parsed.routes[0].match_percent is None
    assert parsed.routes[0].formula_version is None


def test_carousel_takes_the_best_five_in_order() -> None:
    hits = [_hit(95 - index * 5) for index in range(8)]
    block = _catalog_match_block(SimpleNamespace(hits=hits, ideal=[], close=[], formula_version=2))
    assert block is not None
    assert [item.match_percent for item in block.routes] == [95, 90, 85, 80, 75]
