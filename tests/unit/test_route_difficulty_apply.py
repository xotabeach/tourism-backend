"""What the route stores from an estimate (spec 17, D9, D13, section 9)."""

from tourism_backend.modules.routes.application.difficulty import (
    DayInput,
    SegmentInput,
    route_difficulty,
)
from tourism_backend.modules.routes.application.structure import apply_difficulty
from tourism_backend.modules.routes.infrastructure.models import Route


def _estimate(km: float, up: int = 0) -> object:
    segment = SegmentInput(
        mode="walk",
        distance_meters=int(km * 1000),
        ascent_meters=up,
        descent_meters=up,
        terrain_known=True,
    )
    return route_difficulty([DayInput([segment])])


def _route(**fields: object) -> Route:
    route = Route(accessibility={"routing": {"synthetic": False}})
    for name, value in fields.items():
        setattr(route, name, value)
    return route


def test_estimate_is_shown_without_a_rating() -> None:
    route = _route()
    hard = _estimate(9, up=500)
    apply_difficulty(route, hard, _estimate(4))  # type: ignore[arg-type]
    assert (route.difficulty_auto, route.difficulty_reward) == (3, 1)
    assert (route.difficulty_level, route.difficulty) == (3, "moderate")
    assert route.difficulty_confidence == "high"
    assert route.accessibility["difficulty"]["level"] == 3
    # The routing facts next to it stay.
    assert route.accessibility["routing"] == {"synthetic": False}


def test_author_rating_kept_within_one_step_below() -> None:
    route = _route(difficulty_manual=1, difficulty_manual_by="author")
    estimate = _estimate(9, up=500)
    apply_difficulty(route, estimate, estimate)  # type: ignore[arg-type]
    assert route.difficulty_level == 2
    assert route.difficulty_manual == 1


def test_ratings_from_before_the_estimate_show_as_they_are() -> None:
    route = _route(difficulty_manual=4, difficulty_manual_by="legacy")
    estimate = _estimate(3)
    apply_difficulty(route, estimate, estimate)  # type: ignore[arg-type]
    assert (route.difficulty_auto, route.difficulty_level, route.difficulty) == (1, 4, "hard")
