"""AI-01: planning tools only see quality-approved place DTOs."""

from types import SimpleNamespace
from uuid import uuid4

from tourism_backend.modules.route_builder.application.ai_candidates import (
    CANDIDATE_DTO_KEYS,
    DETAIL_DTO_KEYS,
    candidate_dto,
    detail_dto,
    is_ai_approved_place,
    osm_access_forbidden,
)


def _place(**overrides: object) -> SimpleNamespace:
    base = {
        "id": uuid4(),
        "name": "Ливадийский дворец",
        "short_description": "Дворец у моря",
        "publication_status": "published",
        "merged_into_place_id": None,
        "data_quality_status": "editorial_reviewed",
        "freshness_status": "fresh",
        "temporary_closure_status": None,
        "is_suitable_for_children": True,
        "is_suitable_for_pets": None,
        "source_payload": {"tags": {"access": "yes"}},
        "is_paid": False,
        "price_notes": None,
        "recommended_visit_minutes": 90,
        "access_transport": ["walk"],
        "seasonality": ["summer"],
        "typical_crowding": "medium",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_unpublished_and_closed_are_not_approved() -> None:
    assert is_ai_approved_place(_place(), constraints={}) is True
    assert (
        is_ai_approved_place(
            _place(publication_status="draft"),
            constraints={},
        )
        is False
    )
    assert (
        is_ai_approved_place(
            _place(temporary_closure_status="closed"),
            constraints={},
        )
        is False
    )
    assert (
        is_ai_approved_place(
            _place(data_quality_status="rejected"),
            constraints={},
        )
        is False
    )
    assert (
        is_ai_approved_place(
            _place(merged_into_place_id=uuid4()),
            constraints={},
        )
        is False
    )


def test_kids_and_pets_are_hard_constraints() -> None:
    kids_no = _place(is_suitable_for_children=False)
    assert is_ai_approved_place(kids_no, constraints={}) is True
    assert is_ai_approved_place(kids_no, constraints={"with_children": True}) is False
    pets_no = _place(is_suitable_for_pets=False)
    assert is_ai_approved_place(pets_no, constraints={"with_pets": True}) is False


def test_osm_access_no_is_rejected_for_walking() -> None:
    private = _place(source_payload={"tags": {"access": "private"}})
    assert is_ai_approved_place(private, constraints={"transport_mode": "walk"}) is False
    foot_ok = _place(source_payload={"tags": {"access": "private", "foot": "yes"}})
    assert is_ai_approved_place(foot_ok, constraints={"transport_mode": "walk"}) is True
    assert osm_access_forbidden({"access": "no"}, transport_mode="walk") is True
    assert osm_access_forbidden(None, transport_mode="walk") is False


def test_candidate_dto_is_allowlisted_and_bounds_text() -> None:
    place = _place(
        name="A" * 200,
        short_description="<script>alert(1)</script>" + ("x" * 200),
        source_payload={"tags": {"access": "no"}, "secret": "should-not-leak"},
        website_url="https://evil.example",
    )
    dto = candidate_dto(place)
    assert set(dto) == CANDIDATE_DTO_KEYS
    assert len(dto["title"] or "") <= 80
    assert (dto["subtitle"] or "").startswith("<script>")
    assert "secret" not in dto
    assert "website_url" not in dto
    assert "source_payload" not in dto
    assert dto["freshness_status"] == "fresh"
    assert dto["data_quality_status"] == "editorial_reviewed"


def test_detail_dto_omits_coords_hours_and_payload() -> None:
    place = _place(
        opening_hours_raw="Mo-Su 09:00-18:00",
        contact_phone="+7000",
        source_payload={"tags": {}},
    )
    dto = detail_dto(place)
    assert set(dto) == DETAIL_DTO_KEYS
    assert "opening_hours_raw" not in dto
    assert "contact_phone" not in dto
    assert "source_payload" not in dto
    assert "temporary_closure_status" not in dto
    assert dto["visit_minutes"] == 90
