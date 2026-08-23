"""Unit tests for deterministic route catalog scoring."""

from uuid import uuid4

from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn
from tourism_backend.modules.route_builder.application.scoring import (
    RouteMatchCandidate,
    partition_scored,
    score_candidate,
)


def _candidate(**overrides: object) -> RouteMatchCandidate:
    base = {
        "route_id": uuid4(),
        "name": "Ялта · море и дворцы",
        "short_description": "Спокойный день у моря",
        "description": "Ласточкино гнездо и набережная",
        "estimated_duration_minutes": 2_400,
        "difficulty": "easy",
        "transport_mode": "car",
        "seasonality": ("лето", "весна"),
        "suitable_for_children": True,
        "pets_allowed": False,
        "place_names": ("Ласточкино гнездо", "Набережная Ялты"),
        "locality_names": ("Ялта",),
        "stops_count": 4,
    }
    base.update(overrides)
    return RouteMatchCandidate(**base)  # type: ignore[arg-type]


def test_yalta_nature_scores_as_ideal() -> None:
    params = RouteMatchParamsIn(
        city="Ялта",
        trip_type="rest",
        duration="d3_5",
        interests=["Природа", "Пляж"],
        pace="calm",
    )
    scored = score_candidate(params, _candidate())
    assert scored.score >= 0.55
    assert any("Ялта" in reason or "длительность" in reason for reason in scored.reasons)


def test_wrong_city_is_penalized() -> None:
    params = RouteMatchParamsIn(city="Керчь", duration="d3_5", interests=["История"])
    scored = score_candidate(params, _candidate())
    assert scored.score < 0.55


def test_partition_offers_generate_without_ideal() -> None:
    weak = score_candidate(
        RouteMatchParamsIn(city="Керчь", duration="d1_2"),
        _candidate(estimated_duration_minutes=9_000, locality_names=("Ялта",)),
    )
    ideal, close, offer = partition_scored([weak])
    assert ideal == []
    assert offer is True
    assert isinstance(close, list)


def test_transport_aliases_match_walk_and_walking() -> None:
    params = RouteMatchParamsIn(city="Ялта", duration="d1_2", transport_mode="walk")
    scored = score_candidate(
        params,
        _candidate(
            estimated_duration_minutes=300,
            transport_mode="walking",
            seasonality=("лето",),
        ),
    )
    assert any("транспорт" in reason for reason in scored.reasons)
    assert scored.score >= 0.4


def test_season_aliases_accept_english_catalog_values() -> None:
    params = RouteMatchParamsIn(city="Ялта", duration="d3_5", season="лето")
    scored = score_candidate(
        params,
        _candidate(seasonality=("summer", "spring")),
    )
    assert any("сезон" in reason for reason in scored.reasons)


def test_interests_match_by_category_when_text_is_empty() -> None:
    """ADR-009: imported places carry categories but almost no free text.

    A route whose stops are museums/fortresses must match «История» even
    when every descriptive field is empty — the pre-ADR-009 engine scored
    this at the neutral default because it only searched free text.
    """
    params = RouteMatchParamsIn(city="Бахчисарай", duration="d1_2", interests=["История"])
    textless = _candidate(
        name="Маршрут",
        short_description=None,
        description=None,
        place_names=("Объект 1", "Объект 2"),
        locality_names=("Бахчисарай",),
        category_slugs=frozenset({"museum", "fortress"}),
    )
    with_text = score_candidate(params, textless)

    blind = _candidate(
        name="Маршрут",
        short_description=None,
        description=None,
        place_names=("Объект 1", "Объект 2"),
        locality_names=("Бахчисарай",),
        category_slugs=frozenset(),
    )
    without = score_candidate(params, blind)

    assert with_text.score > without.score
    assert any("интересы" in reason for reason in with_text.reasons)


def test_trip_type_matches_by_category_overlap() -> None:
    params = RouteMatchParamsIn(city="Судак", trip_type="adventure", duration="d1_2")
    adventurous = _candidate(
        name="Маршрут",
        short_description=None,
        description=None,
        locality_names=("Судак",),
        category_slugs=frozenset({"cave", "mountain"}),
    )
    scored = score_candidate(params, adventurous)
    assert any("adventure" in reason for reason in scored.reasons)


def test_category_signal_does_not_override_wrong_city() -> None:
    """Taxonomy must not rescue a route in the wrong city (city weight 0.32)."""
    params = RouteMatchParamsIn(city="Керчь", duration="d1_2", interests=["История"])
    scored = score_candidate(
        params,
        _candidate(locality_names=("Ялта",), category_slugs=frozenset({"museum", "fortress"})),
    )
    assert scored.score < 0.55
