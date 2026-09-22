"""Unit tests for deterministic route catalog scoring."""

from uuid import uuid4

from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn
from tourism_backend.modules.route_builder.application.scoring import (
    PARTIAL_DATA_CAP,
    PENALTY_CAP,
    RouteMatchCandidate,
    ScoredMatch,
    UserPreferenceSignals,
    band_of,
    legacy_bands,
    match_percent,
    requested_signal_count,
    score_candidate,
    select_hits,
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
        "stop_coordinates": ((34.17, 44.50),),
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
    outside = ((36.45, 45.35),)
    assert (
        score_candidate(
            params,
            _candidate(locality_names=("Керчь",), stop_coordinates=outside),
        ).score
        == 0
    )
    assert (
        score_candidate(params, _candidate(locality_names=(), stop_coordinates=outside)).score == 0
    )


def test_preferred_towns_rank_softly_not_as_mandatory_stops() -> None:
    params = RouteMatchParamsIn(
        city="Крым", search_area="Южный берег Крыма", preferred_localities=["Форос", "Симеиз"]
    )
    preferred = score_candidate(params, _candidate(locality_names=("Симеиз",)))
    other_coastal = score_candidate(params, _candidate())
    assert preferred.score > other_coastal.score > 0


def test_select_hits_offers_generate_without_ideal() -> None:
    weak = score_candidate(
        RouteMatchParamsIn(city="Керчь", duration="d1_2"),
        _candidate(estimated_duration_minutes=9_000, locality_names=("Ялта",)),
    )
    hits, offer = select_hits([weak])
    assert offer is True
    assert all(band_of(item) == "close" for item in hits)


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


def test_known_incompatible_transport_is_a_penalty_not_an_exclusion() -> None:
    params = RouteMatchParamsIn(city="Ялта", transport_mode="walk")
    wrong = score_candidate(params, _candidate(transport_mode="car"))
    assert 0 < wrong.score <= PENALTY_CAP
    assert wrong.has_violation is True
    assert wrong.mismatches[0] == "другой вид транспорта"
    unknown = score_candidate(params, _candidate(transport_mode=None))
    assert unknown.score > wrong.score
    assert unknown.has_violation is False


def test_explicit_pet_constraint_beats_other_match_signals() -> None:
    params = RouteMatchParamsIn(city="Ялта", with_pets=True)
    excluded = score_candidate(params, _candidate(pets_allowed=False))
    assert excluded.score == 0
    assert excluded.excluded is True
    unknown = score_candidate(params, _candidate(pets_allowed=None))
    assert 0 < unknown.score <= PARTIAL_DATA_CAP
    assert unknown.partial_data is True
    assert unknown.has_violation is True
    assert unknown.mismatches[0] == "нет данных о том, можно ли с питомцами"
    assert band_of(unknown) == "close"


def test_unknown_child_safety_is_shown_with_a_note_and_a_ceiling() -> None:
    params = RouteMatchParamsIn(city="Ялта", with_children=True)
    unknown = score_candidate(params, _candidate(suitable_for_children=None))
    assert unknown.score <= PARTIAL_DATA_CAP
    assert unknown.mismatches[0] == "нет данных о пригодности для детей"
    known = score_candidate(params, _candidate(suitable_for_children=True))
    assert known.score > unknown.score
    assert score_candidate(params, _candidate(suitable_for_children=False)).excluded is True


def test_crowd_preference_changes_order_without_inventing_unknown_data() -> None:
    params = RouteMatchParamsIn(city="Ялта", avoid_crowds=True)
    low = score_candidate(params, _candidate(typical_crowding="low"))
    unknown = score_candidate(params, _candidate(typical_crowding="unknown"))
    high = score_candidate(params, _candidate(typical_crowding="high"))
    # Unknown data is left out of the average, not invented: never better than a
    # known good route, and better than a known bad one.
    assert low.score >= unknown.score > high.score
    assert high.mismatches[0] == "обычно много людей"
    assert not any("мало людей" in reason for reason in unknown.reasons)


def test_daily_budget_affects_known_cost_ranking() -> None:
    params = RouteMatchParamsIn(city="Ялта", budget_amount=2000)
    affordable = score_candidate(params, _candidate(price_min_amount=500))
    expensive = score_candidate(params, _candidate(price_min_amount=100_000))
    assert affordable.score > expensive.score
    free_only = RouteMatchParamsIn(city="Ялта", paid_ok=False)
    paid = score_candidate(free_only, _candidate(price_min_amount=500))
    assert 0 < paid.score <= PENALTY_CAP
    assert paid.has_violation is True
    assert "есть платные места" in paid.mismatches
    assert score_candidate(free_only, _candidate(price_min_amount=None)).has_violation is False


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


# --- spec 06: honest percent -------------------------------------------------


def test_interest_score_saturates_instead_of_diluting() -> None:
    params_many = RouteMatchParamsIn(
        city="Ялта", interests=["История", "Пляж", "Горы", "Еда", "Спорт", "Лошади"]
    )
    params_one = RouteMatchParamsIn(city="Ялта", interests=["История"])
    candidate = _candidate(category_slugs=frozenset({"museum", "beach", "mountain"}))
    many = score_candidate(params_many, candidate)
    one = score_candidate(params_one, candidate)
    # Three of six hit is now about as good as one of one, not half as good.
    assert many.score >= one.score - 0.1
    assert score_candidate(params_many, _candidate(category_slugs=frozenset())).score < many.score


def test_start_mismatch_no_longer_halves_the_score() -> None:
    params = RouteMatchParamsIn(city="Керчь", duration="d1_2", interests=["История"])
    scored = score_candidate(
        params,
        _candidate(estimated_duration_minutes=600, category_slugs=frozenset({"museum"})),
    )
    assert scored.mismatches[0] == "старт не совпал"
    # Start 0.05*0.32, duration 1.0*0.18, interests 1.0*0.2, pace 1.0*0.08 over 0.78.
    assert 0.5 < scored.score < 0.7


def test_unknown_route_data_is_left_out_and_caps_at_partial() -> None:
    params = RouteMatchParamsIn(
        city="Ялта", duration="d1_2", transport_mode="car", season="лето", pace="calm"
    )
    bare = _candidate(
        estimated_duration_minutes=None,
        difficulty=None,
        transport_mode=None,
        seasonality=(),
    )
    scored = score_candidate(params, bare)
    assert scored.partial_data is True
    assert scored.score <= PARTIAL_DATA_CAP
    assert band_of(scored) == "close"
    full = score_candidate(params, _candidate(estimated_duration_minutes=600))
    assert full.partial_data is False
    assert full.score > scored.score


def test_form_defaults_the_person_did_not_choose_are_not_requested() -> None:
    params = RouteMatchParamsIn(city="Ялта", explicit_fields=[])
    unknown_duration = _candidate(estimated_duration_minutes=None)
    with_explicit = score_candidate(params, unknown_duration, explicit_fields=[])
    assert with_explicit.partial_data is False
    assert requested_signal_count(params, explicit_fields=[]) == 1
    assert requested_signal_count(params) == 3  # an older client: start, duration, pace
    assert requested_signal_count(params, explicit_fields=["duration"]) == 2


def test_percent_is_rounded_down_and_full_only_for_a_complete_match() -> None:
    params = RouteMatchParamsIn(city="Ялта", duration="d1_2")
    complete = score_candidate(params, _candidate(estimated_duration_minutes=600))
    assert complete.full_match is True
    assert match_percent(complete) == 100
    shorter = score_candidate(
        RouteMatchParamsIn(city="Ялта", duration="d3_5"), _candidate(estimated_duration_minutes=240)
    )
    assert shorter.full_match is False
    assert match_percent(shorter) <= 95
    assert match_percent(shorter) % 5 == 0


def _hit(score: float, *, violation: bool = False, name: str = "M") -> ScoredMatch:
    return ScoredMatch(
        candidate=_candidate(name=name),
        score=score,
        reasons=(),
        has_violation=violation,
    )


def test_select_hits_applies_floor_cap_order_and_tiebreak() -> None:
    scored = [_hit(0.29, name="низкий")] + [_hit(0.5 + i * 0.01, name=f"М{i}") for i in range(9)]
    scored.append(_hit(0.7, name="Б"))
    scored.append(_hit(0.7, name="А"))
    hits, offer = select_hits(
        scored,
        tiebreak=lambda item: (1.0 if item.candidate.name == "Б" else 0.0, item.candidate.name),
    )
    assert len(hits) == 8
    assert [item.candidate.name for item in hits[:2]] == ["Б", "А"]
    assert all(item.score >= 0.30 for item in hits)
    assert offer is False
    only_low = select_hits([_hit(0.4)])
    assert only_low[1] is True
    assert select_hits([_hit(0.2)]) == ([], True)


def test_excluded_routes_never_reach_the_list() -> None:
    params = RouteMatchParamsIn(city="Ялта", with_children=True)
    excluded = score_candidate(params, _candidate(suitable_for_children=False))
    assert select_hits([excluded])[0] == []


def test_legacy_arrays_keep_the_old_shape_without_violations() -> None:
    hits = [
        _hit(0.9),
        _hit(0.8),
        _hit(0.7),
        _hit(0.65),
        _hit(0.5, violation=True),
        _hit(0.45),
        _hit(0.4),
        _hit(0.36),
        _hit(0.33),
    ]
    ideal, close = legacy_bands(hits)
    assert [item.score for item in ideal] == [0.9, 0.8, 0.7]
    assert [item.score for item in close] == [0.45, 0.4, 0.36]
    assert all(not item.has_violation for item in ideal + close)


def test_chat_counts_only_confirmed_parameters() -> None:
    params = RouteMatchParamsIn(
        city="Ялта", search_area="Южный берег Крыма", interests=["История"], trip_type="rest"
    )
    assert requested_signal_count(params, confirmed_fields=["interests"]) == 2
    assert requested_signal_count(params, confirmed_fields=["search_area", "duration"]) == 4


def test_interest_score_is_judged_against_what_was_asked() -> None:
    from tourism_backend.modules.route_builder.application.scoring import _interest_score

    assert _interest_score(1, 1) == 1.0
    assert _interest_score(3, 6) == 1.0
    assert 0.55 < _interest_score(1, 3) < 0.65
    assert _interest_score(0, 4) == 0.0
    assert _interest_score(2, 2) == 1.0
    assert _interest_score(1, 2) < _interest_score(2, 2)


def test_sea_tag_answers_the_sea_interest_without_text_or_beach() -> None:
    """BACKEND-19: an editor's «Море» tag is enough for «Море» and «Пляж»."""
    textless = {
        "name": "Маршрут",
        "short_description": None,
        "description": None,
        "place_names": ("Объект 1", "Объект 2"),
        "locality_names": ("Ялта",),
        "category_slugs": frozenset({"museum"}),
    }
    for interest in ("Море", "Пляж"):
        params = RouteMatchParamsIn(city="Ялта", duration="d1_2", interests=[interest])
        tagged = score_candidate(params, _candidate(**textless, is_seaside=True))
        untagged = score_candidate(params, _candidate(**textless))
        assert tagged.score > untagged.score
        assert "нет совпадений по интересам" not in tagged.mismatches
        assert "нет совпадений по интересам" in untagged.mismatches
