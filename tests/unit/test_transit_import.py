from tourism_backend.modules.transit.infrastructure.importer import parse_transit


def _variant(osm_id: int, **extra: object) -> dict[str, object]:
    return {
        "osm_id": osm_id,
        "kind": "trolleybus",
        "ptv2": True,
        "ref": "51",
        "name": f"Троллейбус 51 вариант {osm_id}",
        "stops": [
            {"osm_id": "n1", "role": "stop", "lng": 34.1, "lat": 44.9, "name": "Вокзал"},
            {"osm_id": "n2", "role": "stop", "lng": 34.2, "lat": 44.8, "name": "Перевальное"},
        ],
        "shape": [[34.1, 44.9], [34.2, 44.8]],
        **extra,
    }


def test_variants_group_into_lines_by_route_master() -> None:
    parsed = parse_transit(
        {
            "variants": [_variant(10), _variant(11), _variant(12, ref="7", name=None)],
            "masters": [{"osm_id": 5, "ref": "51", "name": "Троллейбус 51", "routes": [10, 11]}],
        }
    )

    by_id = {line.osm_id: line for line in parsed.lines}
    assert set(by_id) == {"m5", "v12"}
    assert [variant.osm_id for variant in by_id["m5"].variants] == ["r10", "r11"]
    assert by_id["m5"].name == "Троллейбус 51"
    # A lone route without a name is named by its kind and number.
    assert by_id["v12"].name == "trolleybus 7"
    # Stops shared by variants are one stop.
    assert [stop.osm_id for stop in parsed.stops] == ["n1", "n2"]


def test_lines_without_ptv2_or_stops_need_mapping() -> None:
    parsed = parse_transit(
        {
            "variants": [
                _variant(20, ptv2=False),
                _variant(21, stops=[]),
                _variant(22),
            ],
            "masters": [{"osm_id": 6, "routes": [20, 21]}],
        }
    )

    by_id = {line.osm_id: line for line in parsed.lines}
    assert by_id["m6"].needs_mapping
    assert not by_id["v22"].needs_mapping


def test_unknown_kinds_and_short_shapes() -> None:
    parsed = parse_transit(
        {
            "variants": [_variant(30, kind="subway"), _variant(31, shape=[[34.1, 44.9]])],
            "masters": [],
        }
    )

    assert [line.osm_id for line in parsed.lines] == ["v31"]
    assert parsed.lines[0].variants[0].shape == [(34.1, 44.9)]
