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


def test_season_and_transport_boost() -> None:
    params = RouteMatchParamsIn(
        city="Ялта",
        duration="d3_5",
        season="лето",
        transport_mode="car",
        with_children=True,
    )
    scored = score_candidate(params, _candidate())
    assert scored.score >= 0.55
    assert any("сезон" in r or "транспорт" in r or "детьми" in r for r in scored.reasons)
