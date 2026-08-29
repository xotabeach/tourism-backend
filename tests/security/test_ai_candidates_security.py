"""Security regressions for AI planning candidate DTOs (AI-01)."""

from types import SimpleNamespace
from uuid import uuid4

from tourism_backend.modules.route_builder.application.ai_candidates import (
    CANDIDATE_DTO_KEYS,
    candidate_dto,
    is_ai_approved_place,
)


def test_sqli_like_city_name_is_plain_data_in_dto() -> None:
    place = SimpleNamespace(
        id=uuid4(),
        name="'; DROP TABLE places;--",
        short_description="1 OR 1=1",
        publication_status="published",
        merged_into_place_id=None,
        data_quality_status="editorial_reviewed",
        freshness_status="fresh",
        temporary_closure_status=None,
        is_suitable_for_children=None,
        is_suitable_for_pets=None,
        source_payload=None,
    )
    assert is_ai_approved_place(place, constraints={}) is True
    dto = candidate_dto(place)
    assert dto["title"] == "'; DROP TABLE places;--"
    assert set(dto) == CANDIDATE_DTO_KEYS


def test_unpublished_uuid_is_not_approved_even_if_known() -> None:
    hidden = SimpleNamespace(
        id=uuid4(),
        name="Секретная точка",
        short_description="не для каталога",
        publication_status="draft",
        merged_into_place_id=None,
        data_quality_status="editorial_reviewed",
        freshness_status="fresh",
        temporary_closure_status=None,
        is_suitable_for_children=True,
        is_suitable_for_pets=True,
        source_payload=None,
    )
    assert is_ai_approved_place(hidden, constraints={}) is False


def test_oversized_untrusted_fields_are_clamped() -> None:
    place = SimpleNamespace(
        id=uuid4(),
        name="Я" * 500,
        short_description="<svg/onload=alert(1)>" * 80,
        publication_status="published",
        merged_into_place_id=None,
        data_quality_status="needs_review",
        freshness_status="stale",
        temporary_closure_status=None,
        is_suitable_for_children=None,
        is_suitable_for_pets=None,
        source_payload={"tags": {}, "raw": "x" * 10_000},
    )
    dto = candidate_dto(place)
    assert len(dto["title"] or "") == 80
    assert len(dto["subtitle"] or "") == 120
    assert dto["freshness_status"] == "stale"
    assert "raw" not in dto
