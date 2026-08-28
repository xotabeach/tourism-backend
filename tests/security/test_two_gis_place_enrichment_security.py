"""Security regressions for 2GIS place catalog matching (GIS-06).

This is a maintenance script surface, not a public API. Vendor JSON is still
untrusted: it must not become HTML, SQL, a publish action, or a secret leak.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tourism_backend.modules.places.application.two_gis_enrichment import (
    ALLOWED_APPLY_FIELDS,
    CatalogHit,
    PlaceProbe,
    build_apply_patch,
    decide_match,
    parse_catalog_items,
    sanitized_report_row,
)

SQLI_LIKE = "' OR '1'='1"
XSS_LIKE = "<script>alert(1)</script>"


def _probe(**changes: object) -> PlaceProbe:
    values: dict[str, object] = {
        "place_id": uuid4(),
        "name": "Ливадийский дворец",
        "lng": 34.1556,
        "lat": 44.4678,
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
        "provider_id": "700000010001",
        "name": "Ливадийский дворец",
        "lng": 34.1557,
        "lat": 44.4679,
        "address": "Ливадия",
        "opening_hours": "Mo-Su 10:00-18:00",
        "rubric_names": ("Музей",),
    }
    values.update(changes)
    return CatalogHit(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [SQLI_LIKE, XSS_LIKE, "javascript:alert(1)"])
def test_untrusted_catalog_strings_stay_data(payload: str) -> None:
    hits = parse_catalog_items(
        {
            "result": {
                "items": [
                    {
                        "id": payload,
                        "name": payload,
                        "point": {"lat": 44.4678, "lon": 34.1556},
                        "address_name": payload,
                    }
                ]
            }
        }
    )
    assert len(hits) == 1
    assert hits[0].name == payload[:255]
    assert hits[0].address == payload[:500]
    row = sanitized_report_row(_probe(name=payload), decide_match(_probe(name=payload), hits))
    dumped = str(row)
    assert "publication_status" not in row
    assert "SELECT " not in dumped.upper().replace(payload.upper(), "")


def test_apply_cannot_publish_or_move_a_place() -> None:
    patch = build_apply_patch(_probe(), decide_match(_probe(), (_hit(),)))
    assert patch is not None
    assert set(patch.fields).issubset(ALLOWED_APPLY_FIELDS)
    assert "publication_status" not in patch.fields
    assert "location" not in patch.fields
    assert "name" not in patch.fields
    assert "opening_hours_raw" not in patch.fields
    assert "safety_warnings" not in patch.fields


def test_report_omits_coordinates_and_raw_vendor_blob() -> None:
    row = sanitized_report_row(_probe(), decide_match(_probe(), (_hit(),)))
    assert "lng" not in row
    assert "lat" not in row
    assert "source_payload" not in row
    assert "point" not in row
    assert "key" not in row
