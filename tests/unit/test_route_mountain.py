"""«Горный маршрут» with a margin (BACKEND-39, spec 15 D8/D21)."""

from tourism_backend.modules.routes.application.mountain import (
    KEY_MIN_ASCENT,
    KEY_MIN_LEVEL,
    MountainRules,
    decide,
    parse_rules,
    validate_setting,
)

RULES = MountainRules(min_level=4, min_ascent_m=500)


def _decide(**kwargs: object) -> tuple[bool, str]:
    base: dict[str, object] = {
        "manual": None,
        "level": 2,
        "ascent_m": 100,
        "reliable": True,
        "outside_settlements": False,
        "rules": RULES,
    }
    status = decide(**{**base, **kwargs})  # type: ignore[arg-type]
    return status.is_mountain, status.source


def test_moderator_choice_wins() -> None:
    assert _decide(manual=False, level=5, ascent_m=1500) == (False, "manual")
    assert _decide(manual=True) == (True, "manual")


def test_hard_or_high_routes_are_mountainous() -> None:
    assert _decide(level=4) == (True, "level")
    assert _decide(ascent_m=500) == (True, "ascent")
    assert _decide(level=3, ascent_m=499) == (False, "none")


def test_unreliable_estimate_outside_towns_warns_with_a_margin() -> None:
    assert _decide(reliable=False, outside_settlements=True) == (True, "unreliable")
    assert _decide(ascent_m=None, outside_settlements=True) == (True, "unreliable")
    # In town an unreliable estimate is not a reason by itself.
    assert _decide(reliable=False, outside_settlements=False) == (False, "none")
    # A reliable easy route outside town is not mountainous.
    assert _decide(outside_settlements=True) == (False, "none")


def test_thresholds_are_bounded_and_a_bad_value_falls_back() -> None:
    assert validate_setting(KEY_MIN_LEVEL, " 3 ") == "3"
    for bad in ("1", "6", "x", ""):
        try:
            validate_setting(KEY_MIN_LEVEL, bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    rules = parse_rules({KEY_MIN_LEVEL: "9", KEY_MIN_ASCENT: "800"})
    assert rules == MountainRules(min_level=4, min_ascent_m=800)
