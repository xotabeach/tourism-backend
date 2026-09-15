"""Data-driven locality recognition for route planning."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tourism_backend.modules.geography.application.service import (
    _name_score,
    locality_names_mentioned,
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
