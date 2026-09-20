"""Anti-fraud settings: bounds, defaults and safe fallbacks."""

from __future__ import annotations

import pytest

from tourism_backend.modules.route_execution.application.antifraud_settings import (
    ALL_KEYS,
    DEFAULT_LADDER,
    AntiFraudMode,
    AntiFraudSettings,
    parse_ladder,
    parse_settings,
    validate_setting,
)


def test_defaults_match_the_spec_and_start_in_shadow() -> None:
    settings = parse_settings({})
    assert settings.mode is AntiFraudMode.SHADOW
    assert settings.enforcing is False
    assert settings.recording is True
    assert settings.pace.ratio == 0.5
    assert settings.pace.min_estimate_seconds == 120
    assert settings.pace.min_mark_ratio == 0.2
    assert settings.pace.min_mark_gap_seconds == 10
    assert settings.escalation.flag_violations == 3
    assert settings.escalation.flag_window_hours == 3
    assert settings.escalation.block_violations == 6
    assert settings.escalation.block_window_hours == 6
    assert settings.escalation.block_ladder_hours == DEFAULT_LADDER
    assert settings.escalation.ladder_memory_days == 30
    assert settings.route_points_cooldown_days == 14
    assert settings.daily_points_cap == 600
    assert settings.gps.tolerance_meters == 150
    assert settings.gps.min_accuracy_meters == 100
    assert settings.hold_overdue_days == 7
    assert settings.batch_window_seconds == 60


def test_valid_overrides_are_applied() -> None:
    settings = parse_settings(
        {
            "af_mode": "enforce",
            "af_pace_violation_ratio": "0.4",
            "af_daily_points_cap": "800",
            "af_block_ladder_hours": "2,48",
        }
    )
    assert settings.enforcing is True
    assert settings.pace.ratio == 0.4
    assert settings.daily_points_cap == 800
    assert settings.escalation.block_ladder_hours == (2, 48)


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("af_flag_violations", "0"),
        ("af_flag_violations", "-3"),
        ("af_flag_violations", "many"),
        ("af_flag_violations", ""),
        ("af_pace_violation_ratio", "1.5"),
        ("af_pace_violation_ratio", "0"),
        ("af_daily_points_cap", "99999"),
    ],
)
def test_out_of_range_values_fall_back_to_defaults(key: str, bad: str) -> None:
    defaults = AntiFraudSettings()
    settings = parse_settings({key: bad})
    assert settings == defaults


def test_bad_mode_and_ladder_fall_back() -> None:
    settings = parse_settings({"af_mode": "turbo", "af_block_ladder_hours": "0,abc"})
    assert settings.mode is AntiFraudMode.SHADOW
    assert settings.escalation.block_ladder_hours == DEFAULT_LADDER


def test_block_threshold_never_below_flag_threshold() -> None:
    settings = parse_settings({"af_flag_violations": "10", "af_block_violations": "4"})
    assert settings.escalation.block_violations == 10


def test_admin_validation_rejects_invalid_and_normalizes_valid() -> None:
    assert validate_setting("af_mode", " Enforce ".lower()) == "enforce"
    assert validate_setting("af_flag_violations", " 4 ") == "4"
    assert validate_setting("af_pace_violation_ratio", "0,45") == "0.45"
    assert validate_setting("af_block_ladder_hours", "1, 24 ,168") == "1,24,168"
    for key, bad in [
        ("af_mode", "on"),
        ("af_flag_violations", "1"),
        ("af_flag_violations", "abc"),
        ("af_pace_violation_ratio", "2"),
        ("af_block_ladder_hours", "1,2,3,4"),
        ("af_unknown", "1"),
    ]:
        with pytest.raises(ValueError, match=".+"):
            validate_setting(key, bad)


def test_ladder_parsing_bounds() -> None:
    assert parse_ladder("1") == (1,)
    with pytest.raises(ValueError, match=".+"):
        parse_ladder("")
    with pytest.raises(ValueError, match=".+"):
        parse_ladder("0")
    with pytest.raises(ValueError, match=".+"):
        parse_ladder("100000")


def test_every_key_is_registered() -> None:
    assert "af_mode" in ALL_KEYS
    assert "af_block_ladder_hours" in ALL_KEYS
    assert all(key.startswith("af_") for key in ALL_KEYS)
