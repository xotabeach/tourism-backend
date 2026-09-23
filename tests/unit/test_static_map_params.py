"""Regression coverage for the 2GIS static map query params.

The static maps API only accepts predefined short color codes on ``pt``
markers (be/rd/oe/yw/gn/pe/pk/gy/bk), unlike ``ls`` polylines which accept an
arbitrary hex RRGGBB. Sending a hex color on ``pt`` is rejected with a plain
400 from 2GIS, which surfaced as a production ``map_preview_upstream_error``.

``pt`` markers must sit on the real stop coordinates, not on points sampled
from the road-following ``ls`` geometry — those follow the road, not the
stop, and produced numbered pins that drifted away from the actual places
shown in the app's own stop list.
"""

from __future__ import annotations

import re

from tourism_backend.modules.maps.presentation.router import _route_static_params

_VALID_PT_COLORS = {"be", "rd", "oe", "yw", "gn", "pe", "pk", "gy", "bk"}
_HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")


def test_route_static_params_use_valid_pt_color_codes() -> None:
    line = [(34.41, 44.68), (34.41, 44.75), (34.20, 44.60)]
    stops = [(34.41, 44.68), (34.20, 44.60)]
    params = _route_static_params(line, stops, width=880, height=420, scale=2)

    pt_values = [value for key, value in params if key == "pt"]
    assert pt_values, "expected at least one pt marker"
    for value in pt_values:
        color = value.split("~c:")[1].split("~")[0]
        assert color in _VALID_PT_COLORS, f"pt color {color!r} is not a predefined 2GIS code"


def test_route_static_params_line_uses_hex_color() -> None:
    line = [(34.41, 44.68), (34.41, 44.75)]
    stops = [(34.41, 44.68), (34.41, 44.75)]
    params = _route_static_params(line, stops, width=880, height=420, scale=2)

    (ls_value,) = [value for key, value in params if key == "ls"]
    color = ls_value.split("~c:")[1].split("~")[0]
    assert _HEX_COLOR.match(color), f"ls color {color!r} should be a hex RRGGBB value"


def test_route_static_params_size_and_point_count() -> None:
    line = [(34.0 + i * 0.01, 44.0 + i * 0.01) for i in range(200)]
    stops = [(34.0, 44.0), (34.05, 44.05), (34.1, 44.1)]
    params = _route_static_params(line, stops, width=880, height=420, scale=2)

    (size_value,) = [value for key, value in params if key == "s"]
    assert size_value == "880x420@2x"

    pt_values = [value for key, value in params if key == "pt"]
    assert len(pt_values) == len(stops)


def test_route_static_params_markers_use_real_stop_coordinates() -> None:
    # A road-following line whose sampled points would NOT land on the stops.
    line = [(34.0 + i * 0.001, 44.0 + i * 0.001) for i in range(500)]
    stops = [(34.41, 44.68), (34.20, 44.60)]
    params = _route_static_params(line, stops, width=880, height=420, scale=2)

    pt_values = [value for key, value in params if key == "pt"]
    assert len(pt_values) == len(stops)
    for (lon, lat), value in zip(stops, pt_values, strict=True):
        lat_str, rest = value.split(",", 1)
        lon_str = rest.split("~", 1)[0]
        assert float(lat_str) == lat
        assert float(lon_str) == lon


def test_route_static_params_center_is_lat_then_lng() -> None:
    """2GIS `c` takes latitude first; sending lng,lat made every map 502."""
    line = [(34.41, 44.68), (34.20, 44.60)]
    stops = [(34.41, 44.68), (34.20, 44.60)]
    params = _route_static_params(
        line,
        stops,
        width=360,
        height=260,
        scale=2,
        center=(44.64, 34.30),
        zoom=11,
        pins="none",
    )

    (center_value,) = [value for key, value in params if key == "c"]
    assert center_value == "44.640000,34.300000"
    (zoom_value,) = [value for key, value in params if key == "z"]
    assert zoom_value == "11"
    assert not [value for key, value in params if key == "pt"]


def test_long_route_geometry_is_decimated_below_the_url_limit() -> None:
    """A 48km route with 2092 geometry vertices got a 414 from 2GIS.

    Encoding every vertex at full precision produced a ~42KB ``ls`` value,
    over the provider's URL length limit — every request for that route came
    back as an upstream error and the app fell back to a plain pins-only
    preview instead of the real map. 800 points was confirmed OK against the
    live API; the decimation keeps real margin under that.
    """
    from tourism_backend.modules.maps.presentation.router import _MAX_LINE_POINTS

    line = [(34.0 + i * 0.0001, 44.0 + i * 0.0001) for i in range(2092)]
    stops = [line[0], line[-1]]

    params = _route_static_params(line, stops, width=880, height=420, scale=2)

    ls_value = dict(params)["ls"]
    encoded_points = ls_value.split("~")[0].count(",") // 2 + 1
    assert encoded_points <= _MAX_LINE_POINTS


def test_decimation_keeps_the_route_endpoints() -> None:
    from tourism_backend.modules.maps.presentation.router import _downsample

    line = [(float(i), float(i)) for i in range(1000)]

    reduced = _downsample(line, 100)

    assert reduced[0] == line[0]
    assert reduced[-1] == line[-1]
    assert len(reduced) <= 100


def test_decimation_is_a_no_op_under_the_limit() -> None:
    from tourism_backend.modules.maps.presentation.router import _downsample

    line = [(float(i), float(i)) for i in range(10)]

    assert _downsample(line, 400) == line


def _frame_pixels(params: list[tuple[str, str]], key: str) -> list[list[tuple[float, float]]]:
    from tourism_backend.modules.maps.presentation.router import _world_xy

    zoom = int(dict(params)["z"])
    result = []
    for name, value in params:
        if name != key:
            continue
        coords = [float(v) for v in value.split("~", 1)[0].split(",")]
        result.append(
            [_world_xy(lon, lat, zoom) for lat, lon in zip(coords[0::2], coords[1::2], strict=True)]
        )
    return result


# Livadia -> Swallow's nest leg of «Классика Южного берега», framed the way the
# app frames a leg (zoom 13 fits it at 377x600 with the
# app's 56px padding); the rest of the route (to Alupka) lies far outside it.
_ROUTE = [(34.0556, 44.4197), (34.0900, 44.4300), (34.1436, 44.4678), (34.1235, 44.4307)]
_STOPS = [_ROUTE[0], _ROUTE[2], _ROUTE[3]]
_LEG_CENTER = (44.44925, 34.13355)


def test_zoomed_frame_only_sends_the_line_inside_it() -> None:
    """2GIS answers 400 "object is out of bounds" to any line leaving the frame."""
    from tourism_backend.modules.maps.presentation.router import _world_xy

    params = _route_static_params(
        _ROUTE,
        _STOPS,
        width=377,
        height=600,
        scale=2,
        center=_LEG_CENTER,
        zoom=13,
        pins="numbered",
    )
    cx, cy = _world_xy(_LEG_CENTER[1], _LEG_CENTER[0], 13)
    lines = _frame_pixels(params, "ls")
    assert lines, "the leg itself must still be drawn"
    for line in lines + _frame_pixels(params, "pt"):
        for x, y in line:
            assert abs(x - cx) <= 377 / 2 - 1.9
            assert abs(y - cy) <= 600 / 2 - 1.9
    # Vorontsov palace (stop 1) is outside the frame, so its pin is dropped.
    assert [v.rsplit("~n:", 1)[1] for k, v in params if k == "pt"] == ["2", "3"]


def test_line_leaving_the_frame_ends_on_its_edge() -> None:
    from tourism_backend.modules.maps.presentation.router import _world_xy

    params = _route_static_params(
        _ROUTE, _ROUTE, width=377, height=600, scale=2, center=_LEG_CENTER, zoom=13, pins="none"
    )
    cx, _ = _world_xy(_LEG_CENTER[1], _LEG_CENTER[0], 13)
    (line,) = _frame_pixels(params, "ls")
    # The Alupka side enters through the left edge (inset by 2px).
    assert abs(line[0][0] - (cx - 377 / 2 + 2)) < 0.01


def test_line_that_leaves_and_returns_becomes_two_pieces() -> None:
    center = (44.45, 34.13)
    zigzag = [(34.125, 44.45), (34.30, 44.45), (34.135, 44.451)]
    params = _route_static_params(
        zigzag, zigzag, width=377, height=600, scale=2, center=center, zoom=14, pins="none"
    )
    assert len([k for k, _ in params if k == "ls"]) == 2


def test_without_a_frame_the_whole_line_is_sent() -> None:
    params = _route_static_params(_ROUTE, _ROUTE, width=377, height=600, scale=2)
    (line,) = [v for k, v in params if k == "ls"]
    assert line.count(",") == len(_ROUTE) * 2 - 1
