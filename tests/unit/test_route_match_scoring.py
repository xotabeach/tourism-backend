"""Unit tests for deterministic route catalog scoring."""

from uuid import uuid4

from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn
from tourism_backend.modules.route_builder.application.scoring import (
    RouteMatchCandidate,
    UserPreferenceSignals,
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


def test_discovery_area_ignores_draft_city_duration_and_pace() -> None:
    params = RouteMatchParamsIn(city="Керчь", search_area="Южный берег Крыма")
    score = score_candidate(params, _candidate(), confirmed_fields=["search_area"])
    changed = params.model_copy(update={"duration": "d7plus", "pace": "active"})
    assert score.score >= 0.55
    assert (
        score.score
        == score_candidate(changed, _candidate(), confirmed_fields=["search_area"]).score
    )


def test_discovery_excludes_outside_localities_even_with_coastal_title() -> None:
    params = RouteMatchParamsIn(city="Ялта", search_area="Южный берег Крыма")
    assert score_candidate(params, _candidate(locality_names=("Керчь",))).score == 0
    assert score_candidate(params, _candidate(locality_names=())).score == 0


def test_preferred_towns_rank_softly_not_as_mandatory_stops() -> None:
    params = RouteMatchParamsIn(
        city="Крым", search_area="Южный берег Крыма", preferred_localities=["Форос", "Симеиз"]
    )
    preferred = score_candidate(params, _candidate(locality_names=("Симеиз",)))
    other_coastal = score_candidate(params, _candidate())
    assert preferred.score > other_coastal.score > 0


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


def test_known_incompatible_transport_is_excluded() -> None:
    params = RouteMatchParamsIn(city="Ялта", transport_mode="walk")
    assert score_candidate(params, _candidate(transport_mode="car")).score == 0
    assert score_candidate(params, _candidate(transport_mode=None)).score > 0


def test_explicit_pet_constraint_beats_other_match_signals() -> None:
    params = RouteMatchParamsIn(city="Ялта", with_pets=True)
    assert score_candidate(params, _candidate(pets_allowed=False)).score == 0
    assert score_candidate(params, _candidate(pets_allowed=None)).score > 0


def test_crowd_preference_changes_order_without_inventing_unknown_data() -> None:
    params = RouteMatchParamsIn(city="Ялта", avoid_crowds=True)
    low = score_candidate(params, _candidate(typical_crowding="low"))
    unknown = score_candidate(params, _candidate(typical_crowding="unknown"))
    high = score_candidate(params, _candidate(typical_crowding="high"))
    assert low.score > unknown.score > high.score
    assert not any("мало людей" in reason for reason in unknown.reasons)


def test_daily_budget_affects_known_cost_ranking() -> None:
    params = RouteMatchParamsIn(city="Ялта", budget_amount=2000)
    affordable = score_candidate(params, _candidate(price_min_amount=500))
    expensive = score_candidate(params, _candidate(price_min_amount=100_000))
    assert affordable.score > expensive.score
    free_only = RouteMatchParamsIn(city="Ялта", paid_ok=False)
    assert score_candidate(free_only, _candidate(price_min_amount=500)).score == 0


def test_short_route_is_an_option_for_part_of_a_long_trip() -> None:
    params = RouteMatchParamsIn(city="Ялта", duration="d3_5")
    scored = score_candidate(params, _candidate(estimated_duration_minutes=240))
    assert scored.score >= 0.55
    assert "можно пройти за часть поездки" in scored.reasons


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


def test_profile_preferences_are_soft_and_explainable() -> None:
    params = RouteMatchParamsIn(city="Ялта", duration="d3_5")
    preferences = UserPreferenceSignals(
        categories=frozenset({"Море"}),
        difficulty="easy",
        travels_with_kids=True,
    )
    preferred = score_candidate(
        params,
        _candidate(category_slugs=frozenset({"beach"})),
        preferences,
    )
    other = score_candidate(
        params,
        _candidate(
            category_slugs=frozenset({"mountain"}),
            difficulty="hard",
            suitable_for_children=False,
        ),
        preferences,
    )
    assert preferred.score > other.score
    assert any("предпочтения" in reason for reason in preferred.reasons)
