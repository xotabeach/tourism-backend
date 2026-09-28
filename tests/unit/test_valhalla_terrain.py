from tourism_backend.modules.route_builder.infrastructure.valhalla_terrain import (
    ground_meters,
    pieces,
)
from tourism_backend.modules.routes.application.terrain_job import split_at_stops


def test_walk_edges_by_grade_and_dirt() -> None:
    edges = [
        {"length": 0.3, "sac_scale": 3, "use": "path", "unpaved": True},
        {"length": 1.2, "sac_scale": 0, "use": "track", "unpaved": True},
        {"length": 0.05, "sac_scale": 0, "use": "steps", "unpaved": False},
        {"length": 2.0, "sac_scale": 0, "use": "road", "unpaved": False},
        {"length": 0},
    ]
    assert ground_meters(edges, mode="walk") == {"T3": 300, "dirt": 1200, "steps": 50}


def test_car_edges_unpaved_offroad_and_hairpins() -> None:
    edges = [
        {"length": 2.0, "unpaved": True, "surface": "dirt", "curvature": 4},
        {"length": 0.4, "unpaved": True, "surface": "path", "curvature": 0},
        {"length": 6.0, "unpaved": False, "surface": "paved_smooth", "curvature": 15},
    ]
    assert ground_meters(edges, mode="car") == {
        "unpaved": 2000,
        "offroad": 400,
        "serpentine": 6000,
    }


def test_long_lines_go_in_overlapping_pieces() -> None:
    # About 1.1 km per step along the parallel: 250 steps is past 100 km.
    line = [[33.0 + index * 0.014, 45.0] for index in range(250)]
    parts = pieces(line)
    assert len(parts) == 3
    assert parts[0][-1] == parts[1][0]
    assert sum(len(part) for part in parts) == len(line) + len(parts) - 1
    assert pieces([[33.0, 45.0]]) == []


def test_split_route_line_at_stops() -> None:
    line = [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0], [4.0, 0.0]]
    legs = split_at_stops(line, [(0.0, 0.1), (2.1, 0.0), (4.0, 0.0)])
    assert legs == {0: line[0:3], 1: line[2:5]}
