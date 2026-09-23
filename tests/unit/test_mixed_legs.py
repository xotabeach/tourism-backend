"""Drive to a car park, walk the rest (spec 14b, section 2)."""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import replace
from uuid import uuid4

import pytest
from PIL import Image

from tourism_backend.modules.maps.infrastructure.osm_static import (
    MapFrame,
    draw_overlays,
    fit_frame,
    to_pixel,
)
from tourism_backend.modules.route_builder.application.mixed_legs import build_driven_route
from tourism_backend.modules.route_builder.application.polyline import decode_polyline6
from tourism_backend.modules.route_builder.application.routing import (
    RouteLegResult,
    RouteWaypoint,
    RoutingError,
    RoutingResult,
    TransportMode,
    routing_details,
)
from tourism_backend.modules.route_builder.infrastructure.parkings import (
    ParkingIndex,
    ParkingPoint,
    parse_features,
)
from tourism_backend.modules.routes.application.structure_rules import (
    plan_segments,
    segment_shapes,
)

_PALACE = (33.8813, 44.7485)  # a car reaches it
_FORTRESS = (33.9205, 44.7420)  # on a plateau: the car stops 660 m short
_MONASTERY = (33.8985, 44.7400)
_STREET_END = (33.9122, 44.7430)  # where the car stops for the fortress
_CAR_PARK = (33.9150, 44.7440)


class _FakeRouter:
    """Straight lines, a car that cannot climb to the fortress."""

    def __init__(self, *, failing: set[tuple[str, tuple[float, float]]] | None = None) -> None:
        self.failing = failing or set()
        self.walk_to_fortress = {_CAR_PARK: 900, _STREET_END: 1400}

    async def route(
        self, *, waypoints: list[RouteWaypoint], transport_mode: TransportMode
    ) -> RoutingResult:
        a, b = waypoints
        if (transport_mode, (b.lng, b.lat)) in self.failing:
            raise RoutingError("routing_unreachable", "no path")
        meters = 1000 if transport_mode == "walk" else 5000
        return RoutingResult(
            provider="valhalla",
            synthetic=False,
            legs=(RouteLegResult(0, 1, meters, meters // 2, None),),
            total_distance_meters=meters,
            total_duration_seconds=meters // 2,
            geometry_wkt=f"LINESTRING({a.lng} {a.lat}, {b.lng} {b.lat})",
            elevation_gain_meters=120 if transport_mode == "walk" else 40,
            elevation_loss_meters=5 if transport_mode == "walk" else 40,
        )

    async def locate_car(self, point: RouteWaypoint) -> tuple[float, float] | None:
        return _STREET_END if (point.lng, point.lat) == _FORTRESS else (point.lng, point.lat)

    async def walk_distances(
        self, sources: Sequence[RouteWaypoint], target: RouteWaypoint
    ) -> list[int | None]:
        return [self.walk_to_fortress.get((s.lng, s.lat)) for s in sources]


async def _car_parks(stop: RouteWaypoint) -> list[tuple[float, float]]:
    return [_CAR_PARK] if (stop.lng, stop.lat) == _FORTRESS else []


def _stops(*points: tuple[float, float]) -> list[RouteWaypoint]:
    return [RouteWaypoint(lng=p[0], lat=p[1]) for p in points]


@pytest.mark.asyncio
async def test_a_stop_the_car_cannot_reach_gets_a_walk_there_and_back() -> None:
    route = await build_driven_route(
        _stops(_PALACE, _FORTRESS, _MONASTERY), router=_FakeRouter(), parkings=_car_parks
    )
    shape = [(s.leg_index, s.seq, s.mode, s.role) for s in route.segments]
    assert shape == [
        (0, 0, "car", "main"),
        (0, 1, "walk", "approach"),
        (1, 0, "walk", "return"),
        (1, 1, "car", "main"),
    ]
    drive, approach, back, _ = route.segments
    # The car park with the shorter walk wins over the street end.
    assert drive.points[-1] == _CAR_PARK
    assert approach.points == (_CAR_PARK, _FORTRESS)
    assert back.points == (_FORTRESS, _CAR_PARK)
    # Walking back down climbs what the way up descended.
    assert (back.elevation_gain_meters, back.elevation_loss_meters) == (5, 120)
    # Only walks carry a climb; the drive's does not count.
    assert drive.elevation_gain_meters is None
    assert route.result.elevation_gain_meters == 125
    assert [leg.distance_meters for leg in route.result.legs] == [6000, 6000]
    assert route.warnings == []


@pytest.mark.asyncio
async def test_reachable_stops_are_a_plain_drive() -> None:
    route = await build_driven_route(
        _stops(_PALACE, _MONASTERY), router=_FakeRouter(), parkings=_car_parks
    )
    assert [(s.mode, s.role) for s in route.segments] == [("car", "main")]


@pytest.mark.asyncio
async def test_a_short_walk_from_the_car_park_is_not_a_segment() -> None:
    router = _FakeRouter()
    router.walk_to_fortress = {_CAR_PARK: 250, _STREET_END: 1400}
    route = await build_driven_route(_stops(_PALACE, _FORTRESS), router=router, parkings=_car_parks)
    assert [(s.mode, s.role) for s in route.segments] == [("car", "main")]
    assert route.segments[0].points[-1] == _CAR_PARK


@pytest.mark.asyncio
async def test_a_failed_part_stays_one_straight_segment_and_is_flagged() -> None:
    router = _FakeRouter(failing={("walk", _FORTRESS)})
    route = await build_driven_route(_stops(_PALACE, _FORTRESS), router=router, parkings=_car_parks)
    drive, approach = route.segments
    assert (drive.origin, approach.origin) == ("router", "synthetic")
    assert route.warnings == ["approach_needs_review:"]
    assert not route.result.synthetic


@pytest.mark.asyncio
async def test_no_walkable_car_park_falls_back_to_the_street_end() -> None:
    router = _FakeRouter()
    router.walk_to_fortress = {}
    route = await build_driven_route(_stops(_PALACE, _FORTRESS), router=router, parkings=_car_parks)
    drive, approach = route.segments
    assert drive.points[-1] == _STREET_END
    assert approach.origin == "synthetic"
    assert route.warnings


@pytest.mark.asyncio
async def test_two_stops_off_one_car_park_are_walked_between() -> None:
    near_fortress = (33.9190, 44.7415)

    async def parks(stop: RouteWaypoint) -> list[tuple[float, float]]:
        return [_CAR_PARK]

    class _Router(_FakeRouter):
        async def locate_car(self, point: RouteWaypoint) -> tuple[float, float] | None:
            return _STREET_END

        async def walk_distances(
            self, sources: Sequence[RouteWaypoint], target: RouteWaypoint
        ) -> list[int | None]:
            return [900 if (s.lng, s.lat) == _CAR_PARK else 1400 for s in sources]

    route = await build_driven_route(
        _stops(_FORTRESS, near_fortress), router=_Router(), parkings=parks
    )
    assert [(s.mode, s.role) for s in route.segments] == [("walk", "main")]


@pytest.mark.asyncio
async def test_router_segments_become_the_route_segments() -> None:
    route = await build_driven_route(
        _stops(_PALACE, _FORTRESS, _MONASTERY), router=_FakeRouter(), parkings=_car_parks
    )
    result = replace(route.result, segments=tuple(s.as_meta() for s in route.segments))
    meta = routing_details(result, stop_count=3, data_version="osm1")
    stops = [uuid4(), uuid4(), uuid4()]
    planned = plan_segments(stops, routing=meta, base_mode="car")
    assert [(p.leg_index, p.seq, p.mode, p.role, p.origin) for p in planned] == [
        (0, 0, "car", "main", "router"),
        (0, 1, "walk", "approach", "router"),
        (1, 0, "walk", "return", "router"),
        (1, 1, "car", "main", "router"),
    ]
    assert (planned[1].from_stop_id, planned[1].to_stop_id) == (stops[0], stops[1])
    shapes = segment_shapes(meta)
    assert decode_polyline6(shapes[(0, 1)]) == [_CAR_PARK, _FORTRESS]


def test_router_segments_that_miss_a_leg_are_ignored() -> None:
    stops = [uuid4(), uuid4(), uuid4()]
    only_first = {
        "segments": [{"leg_index": 0, "seq": 0, "mode": "car", "role": "main", "origin": "router"}],
        "legs": [
            {"distance_meters": 10, "duration_seconds": 1},
            {"distance_meters": 20, "duration_seconds": 2},
        ],
    }
    planned = plan_segments(stops, routing=only_first, base_mode="car")
    assert [p.distance_meters for p in planned] == [10, 20]


def test_car_park_features_skip_the_malformed_and_duplicates() -> None:
    rows = parse_features(
        {
            "features": [
                {"geometry": {"coordinates": [34.1, 44.5]}, "properties": {"osm_id": "n1"}},
                {"geometry": {"coordinates": [34.1, 44.5]}, "properties": {"osm_id": "n1"}},
                {"geometry": {"coordinates": [400, 44.5]}, "properties": {"osm_id": "n2"}},
                {"geometry": {}, "properties": {"osm_id": "n3"}},
                {"geometry": {"coordinates": [34.2, 44.6]}, "properties": {"capacity": 5}},
                "junk",
            ]
        }
    )
    assert [row["osm_id"] for row in rows] == ["n1"]


def test_car_park_index_returns_the_nearest_within_the_radius() -> None:
    index = ParkingIndex(
        [
            ParkingPoint("far", 33.95, 44.742),  # ~2.4 km
            ParkingPoint("near", 33.915, 44.744),
            ParkingPoint("mid", 33.905, 44.745),
        ]
    )
    assert [p.osm_id for p in index.nearest(*_FORTRESS)] == ["near", "mid"]
    assert index.nearest(*_FORTRESS, limit=1)[0].osm_id == "near"


def _painted(png: bytes, frame: MapFrame, point: tuple[float, float]) -> tuple[int, int, int]:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    x, y = to_pixel(frame, *point)
    return image.getpixel((round(x * frame.scale), round(y * frame.scale)))


def test_pieces_draw_the_drive_solid_blue_and_the_walk_dashed_green() -> None:
    frame = fit_frame([_PALACE, _FORTRESS], width=377, height=300, scale=2)
    blank = Image.new("RGBA", (377 * 2, 300 * 2), "white")
    png = draw_overlays(
        blank, frame, pieces=[("car", [_PALACE, _CAR_PARK]), ("walk", [_CAR_PARK, _FORTRESS])]
    )
    middle_of_drive = ((_PALACE[0] + _CAR_PARK[0]) / 2, (_PALACE[1] + _CAR_PARK[1]) / 2)
    r, g, b = _painted(png, frame, middle_of_drive)
    assert b > r
    assert b > g
    walk_start = _CAR_PARK
    r, g, b = _painted(png, frame, walk_start)
    assert (r, g, b) != (255, 255, 255)
