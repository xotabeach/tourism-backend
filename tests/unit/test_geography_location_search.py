"""Data-driven locality recognition for route planning."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tourism_backend.modules.geography.application.service import (
    _name_score,
    locality_names_mentioned,
    rejected_localities,
)
from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn


def _locality(name: str, *aliases: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), name=name, aliases=list(aliases))


def test_mentions_are_resolved_from_catalogue_without_a_city_allowlist() -> None:
    catalogue = [
        _locality("Форос"),
        _locality("Симеиз"),
        _locality("Партенит"),
        _locality("Утёс", "Утес"),
    ]

    assert [
        item.name
        for item in locality_names_mentioned(
            "Хочу проехать от Фороса через Симеиз к Партениту",
            catalogue,  # type: ignore[arg-type]
        )
    ] == ["Форос", "Симеиз", "Партенит"]
    assert [
        item.name
        for item in locality_names_mentioned(
            "Начнём у Утеса",
            catalogue,  # type: ignore[arg-type]
        )
    ] == ["Утёс"]


def test_short_common_words_do_not_match_a_locality_by_one_letter() -> None:
    catalogue = [_locality("Ялта"), _locality("Саки")]
    assert (
        locality_names_mentioned(
            "Я хочу спокойную прогулку у моря",
            catalogue,  # type: ignore[arg-type]
        )
        == []
    )


def test_location_suggestion_score_handles_prefixes_and_cases() -> None:
    assert _name_score("Парт", ["Партенит"]) >= 90
    assert _name_score("в Алупке", ["Алупка"]) == 0
    assert _name_score("Алупке", ["Алупка"]) >= 70


def test_match_params_allow_automatic_or_typed_route_endpoints() -> None:
    automatic = RouteMatchParamsIn(flexible_start=True)
    assert automatic.city is None
    assert automatic.effective_start_query is None

    locality_id = uuid4()
    typed = RouteMatchParamsIn(
        start_query="Форос",
        start_locality_id=str(locality_id),
        finish_query="Скала Дива",
    )
    assert typed.effective_start_query == "Форос"
    assert typed.start_locality_id == str(locality_id)

    with pytest.raises(ValidationError):
        RouteMatchParamsIn(start_place_id="not-a-uuid")


_TOWNS = [
    "Алушта",
    "Бахчисарай",
    "Евпатория",
    "Керчь",
    "Саки",
    "Севастополь",
    "Симферополь",
    "Судак",
    "Феодосия",
    "Ялта",
]


def _named(text: str) -> list[str]:
    catalogue = [_locality(name) for name in _TOWNS]
    return [item.name for item in locality_names_mentioned(text, catalogue)]  # type: ignore[arg-type]


def _refused(text: str) -> list[str]:
    catalogue = [_locality(name) for name in _TOWNS]
    return [item.name for item in rejected_localities(text, catalogue)]  # type: ignore[arg-type]


def test_a_town_typed_with_a_slip_is_still_the_town() -> None:
    # The owner's message of 2026-10-09 (BACKEND-64).
    assert _named("привет подбери маршруты по Евратории") == ["Евпатория"]
    assert _named("что посмотреть в бахчисорае") == ["Бахчисарай"]
    # An exact name does not stop a misspelt one beside it.
    assert _named("маршруты в севастополе и симфирополе") == ["Севастополь", "Симферополь"]


def test_slips_are_not_guessed_for_short_names_or_ordinary_words() -> None:
    assert _named("судно на подводных крыльях, сильно устали") == []
    assert _named("сакура и ялик, ялтинский лук") == []


def test_four_letter_towns_are_found_in_their_case_forms() -> None:
    assert _named("отдых в Саках и Ялте") == ["Саки", "Ялта"]
    assert _named("уехать из Ялты") == ["Ялта"]


def test_a_town_named_to_turn_it_down_is_refused() -> None:
    assert _refused("Евпатория, зачем ты Севастополь скинул") == ["Севастополь"]
    assert _refused("при чём тут Севастополь") == ["Севастополь"]
    assert _refused("хочу в Ялту, а не в Алушту") == ["Алушта"]
    assert _refused("покажи Феодосию без Судака") == ["Судак"]


def test_asking_why_a_town_is_missing_asks_for_it() -> None:
    assert _refused("почему нет Евпатории") == []
    assert _refused("хочу в Евпаторию") == []
