import pytest

from tourism_backend.modules.places.application.osm_import import (
    build_overpass_queries,
    build_overpass_query,
    normalize_overpass_payload,
)


def test_normalize_overpass_payload_maps_verified_source_facts() -> None:
    payload = {
        "elements": [
            {
                "type": "node",
                "id": 42,
                "lat": 44.5,
                "lon": 34.1,
                "version": 3,
                "tags": {
                    "name": "Тестовая пещера",
                    "name:ru": "Пещера для теста",
                    "natural": "cave_entrance",
                    "fee": "yes",
                    "wheelchair": "limited",
                    "dog": "no",
                    "addr:city": "Ялта",
                },
            }
        ]
    }

    result = normalize_overpass_payload(payload)

    assert result.input_count == 1
    assert result.rejected == {}
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.source_external_id == "node/42"
    assert candidate.name == "Пещера для теста"
    assert candidate.category_codes == ("cave",)
    assert candidate.payment_status == "paid"
    assert candidate.is_suitable_for_pets is False
    assert candidate.accessibility == {"wheelchair": "limited", "source": "openstreetmap"}
    assert candidate.source_payload["version"] == 3


def test_normalize_overpass_payload_rejects_unusable_records() -> None:
    payload = {
        "elements": [
            {
                "type": "node",
                "id": 1,
                "lat": 44.5,
                "lon": 34.1,
                "tags": {"natural": "peak"},
            },
            {
                "type": "node",
                "id": 2,
                "lat": 44.5,
                "lon": 34.1,
                "tags": {"name": "Магазин", "shop": "convenience"},
            },
            {
                "type": "node",
                "id": 3,
                "lat": 55.75,
                "lon": 37.61,
                "tags": {"name": "Далеко", "tourism": "attraction"},
            },
        ]
    }

    result = normalize_overpass_payload(payload)

    assert result.candidates == ()
    assert result.rejected == {
        "missing_name": 1,
        "outside_candidate_bbox": 1,
        "unsupported_category": 1,
    }


def test_normalize_overpass_payload_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        normalize_overpass_payload({"elements": []}, limit=0)


def test_normalize_overpass_payload_requires_elements_array() -> None:
    with pytest.raises(ValueError, match="elements"):
        normalize_overpass_payload({})


@pytest.mark.parametrize("batch_size", [0, 8])
def test_overpass_queries_reject_invalid_batch_size(batch_size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        build_overpass_queries(batch_size=batch_size)


def test_overpass_query_requires_selector() -> None:
    with pytest.raises(ValueError, match="selector"):
        build_overpass_query(selectors=())


def test_overpass_query_requests_centers_for_supported_tourism_features() -> None:
    query = build_overpass_query()

    assert 'nwr["tourism"' in query
    assert 'nwr["historic"]' in query
    assert "out center tags qt;" in query


def test_overpass_queries_split_selectors_into_small_batches() -> None:
    queries = build_overpass_queries()

    assert len(queries) == 7
    assert all(query.count("nwr[") == 1 for query in queries)


def test_normalize_overpass_payload_balances_categories_before_limit() -> None:
    elements = [
        {
            "type": "node",
            "id": index,
            "lat": 44.5,
            "lon": 34.1,
            "tags": {"name": f"Место {index}", "tourism": "attraction"},
        }
        for index in range(1, 7)
    ]
    elements.extend(
        [
            {
                "type": "node",
                "id": 100,
                "lat": 44.5,
                "lon": 34.1,
                "tags": {"name": "Пещера", "natural": "cave_entrance"},
            },
            {
                "type": "node",
                "id": 101,
                "lat": 44.5,
                "lon": 34.1,
                "tags": {"name": "Водопад", "natural": "waterfall"},
            },
        ]
    )

    result = normalize_overpass_payload({"elements": elements}, limit=4)

    selected_categories = {item.category_codes[0] for item in result.candidates}
    assert selected_categories == {"cave", "landmark", "waterfall"}
    assert result.rejected["not_selected_after_limit"] == 4


@pytest.mark.parametrize(
    ("tags", "expected_categories"),
    [
        ({"tourism": "gallery"}, ("museum",)),
        ({"tourism": "viewpoint"}, ("viewpoint",)),
        ({"historic": "castle"}, ("fortress",)),
        ({"historic": "manor"}, ("palace",)),
        ({"historic": "archaeological_site"}, ("monument",)),
        ({"leisure": "garden"}, ("park",)),
        ({"leisure": "nature_reserve"}, ("nature",)),
        ({"leisure": "beach_resort"}, ("beach",)),
        ({"amenity": "place_of_worship"}, ("religious_site",)),
        ({"craft": "winery"}, ("winery",)),
        ({"information": "trailhead"}, ("trail",)),
    ],
)
def test_normalize_overpass_payload_maps_supported_taxonomy(
    tags: dict[str, str],
    expected_categories: tuple[str, ...],
) -> None:
    result = normalize_overpass_payload(
        {
            "elements": [
                {
                    "type": "way",
                    "id": 900,
                    "center": {"lat": 44.5, "lon": 34.1},
                    "tags": {"name": "Тестовое место", **tags},
                }
            ]
        }
    )

    assert result.candidates[0].category_codes == expected_categories


def test_normalize_overpass_payload_maps_optional_source_values() -> None:
    result = normalize_overpass_payload(
        {
            "elements": [
                {
                    "type": "relation",
                    "id": 901,
                    "center": {"lat": "44.5", "lon": "34.1"},
                    "timestamp": "2026-08-19T00:00:00Z",
                    "changeset": 77,
                    "tags": {
                        "name:en": "Test place",
                        "tourism": "attraction",
                        "fee": "no",
                        "dog": "designated",
                        "addr:street": "Набережная",
                        "addr:housenumber": "1",
                        "addr:place": "Посёлок",
                    },
                }
            ]
        }
    )

    candidate = result.candidates[0]
    assert candidate.payment_status == "free"
    assert candidate.is_suitable_for_pets is True
    assert candidate.address == "Набережная, 1, Посёлок"
    assert candidate.source_payload["timestamp"] == "2026-08-19T00:00:00Z"
    assert candidate.source_payload["changeset"] == 77
    assert candidate.as_dict()["source_external_id"] == "relation/901"


def test_normalize_overpass_payload_reports_malformed_elements() -> None:
    accepted = {
        "type": "node",
        "id": 500,
        "lat": 44.5,
        "lon": 34.1,
        "tags": {"name": "Допустимое место", "tourism": "attraction"},
    }
    payload = {
        "elements": [
            "not-an-object",
            {"type": "area", "id": 1, "tags": {}},
            {"type": "node", "id": 2},
            {
                "type": "node",
                "id": 3,
                "tags": {"name": "Без координат", "tourism": "attraction"},
            },
            {
                "type": "way",
                "id": 4,
                "center": {"lat": None, "lon": 34.1},
                "tags": {"name": "Плохой центр", "tourism": "attraction"},
            },
            {
                "type": "node",
                "id": 5,
                "lat": "bad",
                "lon": 34.1,
                "tags": {"name": "Плохое число", "tourism": "attraction"},
            },
            {
                "type": "node",
                "id": 6,
                "lat": 95,
                "lon": 34.1,
                "tags": {"name": "Вне координат", "tourism": "attraction"},
            },
            accepted,
            accepted,
        ]
    }

    result = normalize_overpass_payload(payload)

    assert len(result.candidates) == 1
    assert result.rejected == {
        "duplicate_identity": 1,
        "invalid_element": 1,
        "invalid_identity": 1,
        "missing_coordinates": 4,
        "missing_tags": 1,
    }
