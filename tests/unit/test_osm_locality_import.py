import pytest

from tourism_backend.modules.geography.application.osm_locality_import import (
    build_locality_overpass_query,
    normalize_locality_overpass_payload,
)


def test_locality_query_covers_non_city_place_types() -> None:
    query = build_locality_overpass_query()

    assert 'nwr["place"' in query
    assert "city" in query
    assert "village" in query
    assert "hamlet" in query
    assert "area(id:3603788824)" in query
    assert "area(id:3601574364)" in query
    assert "out center tags qt;" in query


def test_locality_query_requires_a_valid_region_relation() -> None:
    with pytest.raises(ValueError, match="relation"):
        build_locality_overpass_query(region_relation_ids=())


def test_normalize_locality_prefers_russian_name_and_collects_aliases() -> None:
    result = normalize_locality_overpass_payload(
        {
            "elements": [
                {
                    "type": "node",
                    "id": 42,
                    "lat": 44.4,
                    "lon": 33.9,
                    "version": 7,
                    "tags": {
                        "place": "village",
                        "name": "Simeiz",
                        "name:ru": "Симеиз",
                        "official_name": "посёлок Симеиз",
                        "alt_name": "Симеїз;Симеис",
                        "population": "2 738",
                    },
                }
            ]
        }
    )

    assert result.rejected == {}
    candidate = result.candidates[0]
    assert candidate.name == "Симеиз"
    assert candidate.locality_type == "village"
    assert candidate.population == 2738
    assert candidate.aliases == (
        "Simeiz",
        "посёлок Симеиз",
        "Симеїз",
        "Симеис",
    )
    assert candidate.source_external_id == "node/42"
    assert candidate.source_payload["version"] == 7


def test_normalize_locality_keeps_bbox_candidates_for_editorial_review() -> None:
    result = normalize_locality_overpass_payload(
        {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 44.5,
                    "lon": 34.1,
                    "tags": {"place": "hamlet", "name": "Посёлок"},
                },
                {
                    "type": "node",
                    "id": 2,
                    "lat": 48.0,
                    "lon": 34.1,
                    "tags": {"place": "city", "name": "Далеко"},
                },
                {
                    "type": "node",
                    "id": 3,
                    "lat": 44.5,
                    "lon": 34.1,
                    "tags": {"place": "farm", "name": "Ферма"},
                },
            ]
        }
    )

    assert [candidate.name for candidate in result.candidates] == ["Посёлок"]
    assert result.rejected == {
        "outside_candidate_bbox": 1,
        "unsupported_place_type": 1,
    }


def test_normalize_locality_deduplicates_and_honours_limit() -> None:
    elements = [
        {
            "type": "node",
            "id": index,
            "lat": 44.5,
            "lon": 34.1,
            "tags": {
                "place": "village",
                "name": f"Село {index}",
                "population": str(index),
            },
        }
        for index in range(1, 4)
    ]
    elements.append(elements[0])

    result = normalize_locality_overpass_payload({"elements": elements}, limit=2)

    assert [candidate.name for candidate in result.candidates] == ["Село 3", "Село 2"]
    assert result.rejected == {
        "duplicate_identity": 1,
        "not_selected_after_limit": 1,
    }


def test_normalize_locality_merges_nearby_node_and_relation_but_not_distant_names() -> None:
    result = normalize_locality_overpass_payload(
        {
            "elements": [
                {
                    "type": "node",
                    "id": 10,
                    "lat": 44.6068,
                    "lon": 33.4943,
                    "tags": {
                        "place": "city",
                        "name": "Севастополь",
                        "population": "509992",
                        "wikidata": "Q7525",
                    },
                },
                {
                    "type": "relation",
                    "id": 11,
                    "center": {"lat": 44.6054, "lon": 33.5221},
                    "tags": {
                        "place": "city",
                        "name": "Севастополь",
                        "old_name": "Ахтиар",
                        "population": "458253",
                        "wikidata": "Q7525",
                    },
                },
                {
                    "type": "node",
                    "id": 12,
                    "lat": 45.7,
                    "lon": 35.9,
                    "tags": {"place": "city", "name": "Севастополь"},
                },
            ]
        }
    )

    assert len(result.candidates) == 2
    merged = result.candidates[0]
    assert merged.osm_type == "node"
    assert merged.population == 509992
    assert merged.aliases == ("Ахтиар",)
    assert merged.source_payload["duplicate_osm_ids"] == ["node/10", "relation/11"]
    assert result.rejected == {"duplicate_same_name_nearby": 1}


@pytest.mark.parametrize("limit", [0, 10_001])
def test_normalize_locality_rejects_invalid_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        normalize_locality_overpass_payload({"elements": []}, limit=limit)
