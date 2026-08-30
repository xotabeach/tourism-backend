"""Regression coverage for the 2GIS static map query params.

The static maps API only accepts predefined short color codes on ``pt``
markers (be/rd/oe/yw/gn/pe/pk/gy/bk), unlike ``ls`` polylines which accept an
arbitrary hex RRGGBB. Sending a hex color on ``pt`` is rejected with a plain
400 from 2GIS, which surfaced as a production ``map_preview_upstream_error``.
"""

from __future__ import annotations

import re

from tourism_backend.modules.maps.presentation.router import _route_static_params

_VALID_PT_COLORS = {"be", "rd", "oe", "yw", "gn", "pe", "pk", "gy", "bk"}
_HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")


def test_route_static_params_use_valid_pt_color_codes() -> None:
    points = [(34.41, 44.68), (34.41, 44.75), (34.20, 44.60)]
    params = _route_static_params(points, width=880, height=420, scale=2)

    pt_values = [value for key, value in params if key == "pt"]
    assert pt_values, "expected at least one pt marker"
    for value in pt_values:
        color = value.split("~c:")[1].split("~")[0]
        assert color in _VALID_PT_COLORS, f"pt color {color!r} is not a predefined 2GIS code"


def test_route_static_params_line_uses_hex_color() -> None:
    points = [(34.41, 44.68), (34.41, 44.75)]
    params = _route_static_params(points, width=880, height=420, scale=2)

    (ls_value,) = [value for key, value in params if key == "ls"]
    color = ls_value.split("~c:")[1].split("~")[0]
    assert _HEX_COLOR.match(color), f"ls color {color!r} should be a hex RRGGBB value"


def test_route_static_params_size_and_point_count() -> None:
    points = [(34.0 + i * 0.01, 44.0 + i * 0.01) for i in range(20)]
    params = _route_static_params(points, width=880, height=420, scale=2)

    (size_value,) = [value for key, value in params if key == "s"]
    assert size_value == "880x420@2x"

    pt_values = [value for key, value in params if key == "pt"]
    assert 1 <= len(pt_values) <= 8
