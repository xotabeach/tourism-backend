"""Unit tests for 2GIS catalog matching (GIS-06)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from tourism_backend.modules.places.application.two_gis_enrichment import (
    ALLOWED_APPLY_FIELDS,
    CatalogHit,
    PlaceProbe,
    already_enriched,
    build_apply_patch,
    decide_match,
    parse_catalog_items,
    sanitized_report_row,
)


def _probe(**changes: object) -> PlaceProbe:
    values: dict[str, object] = {
        "place_id": uuid4(),
        "name": "Ласточкино гнездо",
        "lng": 34.0928,
        "lat": 44.4308,
        "address": None,
        "publication_status": "published",
        "source_external_id": "node/1",
        "data_quality_status": "auto_validated",
        "opening_hours_raw": None,
        "source_payload": {"tags": {"tourism": "attraction"}},
    }
    values.update(changes)
    return PlaceProbe(**values)  # type: ignore[arg-type]


def _hit(**changes: object) -> CatalogHit:
    values: dict[str, object] = {
        "provider_id": "700000010123",
        "name": "Ласточкино гнездо",
        "lng": 34.0929,
        "lat": 44.4309,
        "address": "Ялта, Гаспра",
        "opening_hours": "Mo-Su 09:00-18:00",
        "rubric_names": ("Достопримечательность",),
    }
    values.update(changes)
    return CatalogHit(**values)  # type: ignore[arg-type]


def test_parse_catalog_items_ignores_malformed_and_caps_fields() -> None:
    hits = parse_catalog_items(
        {
            "result": {
                "items": [
                    {
                        "id": 700000010123,
                        "name": "Ласточкино гнездо",
                        "point": {"lat": 44.4308, "lon": 34.0928},
                        "address_name": "Гаспра",
                        "rubrics": [{"name": "Достопримечательность"}, {"id": 1}],
                    },
                    {"id": "x", "name": "no point"},
                    {"javascript": "alert(1)"},
                    {
                        "id": "2",
                        "name": "x" * 400,
                        "point": {"lat": 91, "lon": 34.1},
                    },
                ]
            }
        }
    )
    assert len(hits) == 1
    assert hits[0].provider_id == "700000010123"
    assert hits[0].address == "Гаспра"
    assert hits[0].rubric_names == ("Достопримечательность",)


def test_unique_nearby_same_name_is_high_confidence_match() -> None:
    verdict = decide_match(_probe(), (_hit(),))
    assert verdict.decision == "matched"
    assert verdict.confidence == "high"
    assert verdict.distance_m is not None
    assert verdict.distance_m < 80


def test_two_close_named_hits_are_ambiguous() -> None:
    verdict = decide_match(
        _probe(),
        (
            _hit(),
            _hit(provider_id="700000010999", lng=34.0930, lat=44.4310),
        ),
    )
    assert verdict.decision == "ambiguous"
    assert verdict.reason == "multiple_nearby_candidates"


def test_far_unrelated_hit_is_not_found() -> None:
    verdict = decide_match(
        _probe(),
        (_hit(name="АЗС", lng=34.20, lat=44.55, provider_id="1"),),
    )
    assert verdict.decision == "not_found"
    assert verdict.hit is not None
    assert verdict.hit.name == "АЗС"


def test_unique_strong_name_with_coordinate_drift_is_review() -> None:
    verdict = decide_match(
        _probe(),
        (_hit(lng=34.0955, lat=44.4325),),
    )
    assert verdict.decision == "ambiguous"
    assert verdict.confidence == "medium"
    assert verdict.distance_m is not None
    assert 80 < verdict.distance_m < 400


def test_existing_applied_two_gis_id_is_skipped_without_scoring() -> None:
    probe = _probe(source_payload={"two_gis": {"provider_id": "already", "applied": True}})
    assert already_enriched(probe.source_payload) is True
    verdict = decide_match(probe, (_hit(),))
    assert verdict.decision == "skipped"


def test_unapplied_candidate_id_is_not_treated_as_enriched() -> None:
    probe = _probe(source_payload={"two_gis": {"provider_id": "maybe", "applied": False}})
    assert already_enriched(probe.source_payload) is False
    verdict = decide_match(probe, (_hit(),))
    assert verdict.decision == "matched"


def test_apply_patch_fills_empty_address_not_existing_external_id() -> None:
    fetched = datetime(2026, 8, 29, tzinfo=UTC)
    patch = build_apply_patch(_probe(), decide_match(_probe(), (_hit(),)), fetched_at=fetched)
    assert patch is not None
    assert patch.fields["address"] == "Ялта, Гаспра"
    assert "source_external_id" not in patch.fields
    assert patch.payload["two_gis"]["provider_id"] == "700000010123"
    assert patch.payload["two_gis"]["applied"] is True
    assert "name" not in patch.fields
    assert "location" not in patch.fields
    assert "publication_status" not in patch.fields
    assert "opening_hours_raw" not in patch.fields
    assert patch.payload["two_gis"]["proposals"]["opening_hours"] == "Mo-Su 09:00-18:00"
    assert patch.payload["two_gis"]["audit"]["actor"] == "scripts/enrich_places_2gis.py"
    assert "address" in patch.payload["two_gis"]["audit"]["applied_fields"]
    assert "name" in patch.payload["two_gis"]["audit"]["kept_existing"]


def test_apply_patch_does_not_overwrite_existing_address() -> None:
    probe = _probe(address="Редакторский адрес")
    patch = build_apply_patch(probe, decide_match(probe, (_hit(),)))
    assert patch is not None
    assert "address" not in patch.fields


def test_apply_patch_can_set_external_id_when_missing() -> None:
    probe = _probe(source_external_id=None)
    patch = build_apply_patch(probe, decide_match(probe, (_hit(),)))
    assert patch is not None
    assert patch.fields["source_external_id"] == "2gis:700000010123"


def test_ambiguous_apply_marks_needs_review_but_not_editorial_rows() -> None:
    hits = (_hit(), _hit(provider_id="other", lng=34.0930, lat=44.4310))
    auto = _probe(data_quality_status="auto_validated")
    editorial = _probe(data_quality_status="editorial_reviewed")
    auto_patch = build_apply_patch(auto, decide_match(auto, hits))
    editorial_patch = build_apply_patch(editorial, decide_match(editorial, hits))
    assert auto_patch is not None
    assert auto_patch.fields == {"data_quality_status": "needs_review"}
    assert auto_patch.payload["two_gis"]["applied"] is False
    assert editorial_patch is None


def test_report_row_has_no_vendor_blob_or_coordinates() -> None:
    probe = _probe()
    row = sanitized_report_row(probe, decide_match(probe, (_hit(),)))
    assert row["decision"] == "matched"
    assert "point" not in row
    assert "source_payload" not in row
    assert "javascript" not in str(row)


def test_apply_fields_stay_inside_allowlist() -> None:
    patch = build_apply_patch(_probe(), decide_match(_probe(), (_hit(),)))
    assert patch is not None
    assert set(patch.fields).issubset(ALLOWED_APPLY_FIELDS)


def test_xss_like_catalog_name_is_plain_clipped_text() -> None:
    hits = parse_catalog_items(
        {
            "result": {
                "items": [
                    {
                        "id": "xss",
                        "name": "<script>alert(1)</script>",
                        "point": {"lat": 44.4308, "lon": 34.0928},
                    }
                ]
            }
        }
    )
    assert len(hits) == 1
    assert hits[0].name == "<script>alert(1)</script>"
    probe = _probe(name="<script>alert(1)</script>")
    row = sanitized_report_row(probe, decide_match(probe, hits))
    assert row["provider_name"] == "<script>alert(1)</script>"
    assert row["decision"] == "matched"
    assert "html" not in row
