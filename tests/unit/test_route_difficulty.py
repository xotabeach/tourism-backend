import pytest

from tourism_backend.modules.routes.application.difficulty import (
    DayInput,
    SegmentInput,
    day_difficulty,
    effort_level,
    legacy_name,
    level_from_legacy,
    lowest_manual_level,
    reward_multiplier,
    route_difficulty,
    shown_level,
)


def _walk(km: float, *, up: int = 0, down: int = 0, **extra: object) -> SegmentInput:
    return SegmentInput(
        mode="walk",
        distance_meters=int(km * 1000),
        ascent_meters=up,
        descent_meters=down,
        terrain_known=True,
        **extra,  # type: ignore[arg-type]
    )


def _drive(hours: float, **terrain: int) -> SegmentInput:
    return SegmentInput(
        mode="car",
        distance_meters=int(hours * 50_000),
        duration_seconds=int(hours * 3600),
        terrain=terrain,
        terrain_known=True,
    )


def _codes(result: object) -> list[str]:
    return [reason.code for reason in result.reasons]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("effort", "level"),
    [(0, 1), (6, 1), (6.1, 2), (12, 2), (15, 3), (20, 3), (25, 4), (28, 4), (28.5, 5)],
)
def test_effort_thresholds(effort: float, level: int) -> None:
    assert effort_level(effort) == level


def test_city_walk_is_level_one() -> None:
    result = route_difficulty([DayInput([_walk(4.5, up=40, down=40)])])
    assert result.level == 1
    assert result.confidence == "high"


def test_ascent_counts_as_distance() -> None:
    # 9 km with 500 m up and down: 9 + 5 + 2.5 = 16.5 effort km.
    day = day_difficulty(DayInput([_walk(9, up=500, down=500)]))
    assert day.effort_km == 16.5
    assert day.level == 3


def test_hard_trail_sets_the_floor_even_when_short() -> None:
    short_steep = _walk(3, up=150, down=150, terrain={"T3": 400})
    result = route_difficulty([DayInput([short_steep])])
    assert result.level == 4
    assert _codes(result)[0] == "trail"


def test_short_bits_of_hard_ground_do_not_count() -> None:
    result = route_difficulty([DayInput([_walk(3, terrain={"T4": 150, "T1": 2000})])])
    assert result.level == 2


def test_steep_profile_without_tags() -> None:
    result = route_difficulty([DayInput([_walk(3, up=300, max_slope_degrees=31)])])
    assert result.level == 4
    assert "steep" in _codes(result)


def test_palaces_by_car_stay_easy() -> None:
    day = DayInput([_drive(1.5), _walk(0.4), _drive(0.5), _walk(0.6)])
    result = route_difficulty([day])
    assert (result.level, result.walk_level, result.drive_level) == (1, 1, 1)


def test_driving_levels() -> None:
    assert day_difficulty(DayInput([_drive(4)])).level == 2
    assert day_difficulty(DayInput([_drive(1, serpentine=6000)])).level == 2
    assert day_difficulty(DayInput([_drive(6)])).level == 3
    assert day_difficulty(DayInput([_drive(1, unpaved=1200)])).level == 3
    assert day_difficulty(DayInput([_drive(1, unpaved=7000)])).level == 4
    assert day_difficulty(DayInput([_drive(1, offroad=500)])).level == 5


def test_hard_walk_and_hard_drive_add_a_step() -> None:
    day = day_difficulty(DayInput([_drive(1, unpaved=1200), _walk(9, up=500, down=500)]))
    assert (day.walk_level, day.drive_level, day.level) == (3, 3, 4)
    assert "walk_and_drive" in [reason.code for reason in day.reasons]


def test_long_day_with_visits_is_at_least_two() -> None:
    day = day_difficulty(DayInput([_walk(3)], total_minutes=10 * 60))
    assert day.level == 2
    assert day.reasons[-1].code == "long_day"


def test_family_three_days_stays_easy() -> None:
    days = [DayInput([_walk(8, up=100, down=100)]) for _ in range(3)]
    assert route_difficulty(days).level == 2


def test_three_mountain_days_add_one_step() -> None:
    days = [DayInput([_walk(9, up=500, down=500)]) for _ in range(3)]
    result = route_difficulty(days)
    assert result.level == 4
    assert result.reasons[-1].code == "multi_day"
    assert [day.level for day in result.days] == [3, 3, 3]


def test_hardest_day_decides_and_never_exceeds_five() -> None:
    days = [DayInput([_walk(4)]), DayInput([_walk(30, up=1500, down=1500)])] * 2
    assert route_difficulty(days).level == 5


def test_missing_data_lowers_confidence() -> None:
    untagged = SegmentInput(mode="walk", distance_meters=5000)
    assert route_difficulty([DayInput([untagged])]).confidence == "low"
    assert route_difficulty([DayInput([_walk(5)])], synthetic=True).confidence == "low"
    assert route_difficulty([DayInput([_walk(5)])], synthetic=True).reasons[-1].code == "low_data"


def test_no_days_is_level_one() -> None:
    assert route_difficulty([]).level == 1


def test_manual_rating_allowance() -> None:
    assert lowest_manual_level(4) == 3
    assert lowest_manual_level(1) == 1
    assert shown_level(4, None) == 4
    assert shown_level(4, 5) == 5
    assert shown_level(4, 3) == 3
    # Below the allowance the shown level is raised to it (D13).
    assert shown_level(4, 1) == 3
    assert shown_level(4, 1, editorial=True) == 1


def test_legacy_words_and_rewards() -> None:
    assert [legacy_name(level) for level in range(1, 6)] == [
        "easy",
        "easy",
        "moderate",
        "hard",
        "extreme",
    ]
    assert level_from_legacy("Moderate") == 3
    assert level_from_legacy("expert") == 5
    assert level_from_legacy(None) is None
    assert [reward_multiplier(level) for level in (None, 1, 3, 5)] == [1.0, 1.0, 1.25, 1.5]


def test_breakdown_is_serialisable() -> None:
    meta = route_difficulty(
        [DayInput([_walk(9, up=500, down=500, terrain={"T2": 1200})])]
    ).as_meta()
    assert meta["level"] == 3
    assert meta["days"][0]["reasons"][0] == {  # type: ignore[index]
        "code": "walk_effort",
        "effort_km": 16.5,
        "km": 9.0,
        "ascent_m": 500,
        "descent_m": 500,
    }


def test_ungraded_dirt_trail_is_level_two() -> None:
    assert route_difficulty([DayInput([_walk(3, terrain={"dirt": 1500})])]).level == 2
    assert route_difficulty([DayInput([_walk(3, terrain={"dirt": 600})])]).level == 1


def test_quick_estimate_before_saving() -> None:
    from tourism_backend.modules.routes.application.difficulty import quick_estimate

    assert (
        quick_estimate(
            mode="walk",
            distance_meters=9000,
            duration_seconds=10_800,
            elevation_gain_meters=500,
            elevation_loss_meters=500,
        )
        == 3
    )
    assert (
        quick_estimate(
            mode="walk",
            distance_meters=9000,
            duration_seconds=None,
            elevation_gain_meters=None,
            elevation_loss_meters=None,
            synthetic=True,
        )
        is None
    )
