"""Google polyline with 1e6 precision, the shape format Valhalla speaks.

Segment lines are kept in this form in a route's routing metadata: a
100 km drive is a few kilobytes instead of tens of kilobytes of numbers.
"""

from __future__ import annotations

from collections.abc import Sequence

from tourism_backend.modules.route_builder.application.routing import RoutingError


def decode_polyline6(encoded: str) -> list[tuple[float, float]]:
    """(lng, lat) pairs of an encoded line."""
    points: list[tuple[float, float]] = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= length:
                    raise RoutingError("routing_provider_error", "Valhalla shape is truncated")
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        points.append((lng / 1e6, lat / 1e6))
    return points


def encode_polyline6(points: Sequence[tuple[float, float]]) -> str:
    """The inverse of :func:`decode_polyline6` for (lng, lat) pairs."""
    out: list[str] = []
    last_lat = last_lng = 0
    for lng, lat in points:
        ilat, ilng = round(lat * 1e6), round(lng * 1e6)
        for delta in (ilat - last_lat, ilng - last_lng):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        last_lat, last_lng = ilat, ilng
    return "".join(out)
