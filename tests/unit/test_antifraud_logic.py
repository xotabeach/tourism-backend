"""Pure anti-fraud rules: leg estimates, pace, GPS verdict, escalation, soft limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tourism_backend.modules.route_execution.application.antifraud_logic import (
    EscalationRules,
    GpsReading,
    GpsRules,
    GpsVerdict,
    LegEstimate,
    PaceKind,
    PaceRules,
    StopPoint,
    actual_leg_seconds,
    apply_daily_cap,
    build_leg_estimates,
    cooldown_active,
    count_within,
    evaluate_escalation,
    evaluate_pace,
    gps_verdict,
    haversine_meters,
    is_batched,
    moscow_day_bounds,
    next_block_step,
    pace_warn_below_seconds,
    paused_overlap_seconds,
    provider_legs_from_metadata,
    speed_mps_for,
    straight_line_leg,
)

T0 = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)

# Two stops ~1.1 km apart (0.01 degrees of latitude ≈ 1112 m).
A = StopPoint(position=1, lat=44.50, lng=34.00)
B = StopPoint(position=2, lat=44.51, lng=34.00)
C = StopPoint(position=3, lat=44.52, lng=34.00)


def test_haversine_matches_known_distance() -> None:
    assert haversine_meters(44.50, 34.0, 44.51, 34.0) == pytest.approx(1112, abs=5)
    assert haversine_meters(44.5, 34.0, 44.5, 34.0) == 0


def test_speed_depends_on_transport_mode() -> None:
    assert speed_mps_for("walking") < speed_mps_for("bike") < speed_mps_for("public")
    assert speed_mps_for("public") < speed_mps_for("driving")
    assert speed_mps_for(None) == speed_mps_for("walk")
    assert speed_mps_for("unknown-mode") == speed_mps_for("walk")


def test_straight_line_leg_applies_detour_and_speed() -> None:
    leg = straight_line_leg(A, B, transport_mode="walking")
    assert leg is not None
    assert leg.source == "straight_line"
    assert leg.distance_meters == pytest.approx(1112 * 1.3, abs=10)
    assert leg.duration_seconds == pytest.approx(leg.distance_meters / 1.25, abs=2)


def test_straight_line_leg_needs_coordinates() -> None:
    assert straight_line_leg(A, StopPoint(2, None, None), transport_mode="walk") is None


def test_first_stop_has_no_leg_and_order_is_by_position() -> None:
    estimates = build_leg_estimates([C, A, B], transport_mode="walk")
    assert estimates[0] is None
    assert all(item is not None for item in estimates[1:])
    assert len(estimates) == 3


def test_provider_legs_are_preferred_when_they_match_the_stop_count() -> None:
    provider = [LegEstimate(2000, 1500, "provider"), LegEstimate(3000, 2200, "provider")]
    estimates = build_leg_estimates([A, B, C], transport_mode="walk", provider_legs=provider)
    assert estimates[1] == provider[0]
    assert estimates[2] == provider[1]


def test_provider_legs_metadata_is_validated() -> None:
    good = {"legs": [{"distance_meters": 100, "duration_seconds": 60}]}
    parsed = provider_legs_from_metadata(good, stop_count=2)
    assert parsed is not None
    assert parsed[0].source == "provider"
    assert provider_legs_from_metadata(good, stop_count=3) is None
    assert provider_legs_from_metadata({"legs": [{"distance_meters": -1}]}, stop_count=2) is None
    assert provider_legs_from_metadata({"legs": "nope"}, stop_count=2) is None
    assert provider_legs_from_metadata(None, stop_count=2) is None


# ---------------------------------------------------------------- pace


RULES = PaceRules()


def test_first_mark_is_never_evaluated() -> None:
    result = evaluate_pace(estimate_seconds=1800, actual_seconds=None, rules=RULES)
    assert result.kind is PaceKind.SKIPPED
    assert result.below_floor is False


def test_leg_under_the_ratio_is_too_fast() -> None:
    assert (
        evaluate_pace(estimate_seconds=1800, actual_seconds=800, rules=RULES).kind
        is PaceKind.TOO_FAST
    )
    assert evaluate_pace(estimate_seconds=1800, actual_seconds=900, rules=RULES).kind is PaceKind.OK


def test_short_or_missing_estimates_are_skipped() -> None:
    assert (
        evaluate_pace(estimate_seconds=100, actual_seconds=1, rules=RULES).kind is PaceKind.SKIPPED
    )
    assert (
        evaluate_pace(estimate_seconds=None, actual_seconds=1, rules=RULES).kind is PaceKind.SKIPPED
    )
    assert evaluate_pace(estimate_seconds=0, actual_seconds=1, rules=RULES).kind is PaceKind.SKIPPED


def test_floor_removes_stop_points_even_for_skipped_legs() -> None:
    quick = evaluate_pace(estimate_seconds=100, actual_seconds=3, rules=RULES)
    assert quick.kind is PaceKind.SKIPPED
    assert quick.below_floor is True  # 3 s is under the 10 s minimum gap


def test_floor_scales_with_the_estimate() -> None:
    # 20% of 1800 s = 360 s.
    assert evaluate_pace(estimate_seconds=1800, actual_seconds=359, rules=RULES).below_floor
    assert not evaluate_pace(estimate_seconds=1800, actual_seconds=361, rules=RULES).below_floor


def test_actual_time_is_net_of_pauses_and_clamped() -> None:
    assert actual_leg_seconds(previous_mark_at=None, this_mark_at=T0, paused_seconds=0) is None
    assert (
        actual_leg_seconds(
            previous_mark_at=T0, this_mark_at=T0 + timedelta(minutes=30), paused_seconds=600
        )
        == 1200
    )
    assert (
        actual_leg_seconds(
            previous_mark_at=T0, this_mark_at=T0 + timedelta(minutes=1), paused_seconds=900
        )
        == 0
    )


def test_paused_overlap_counts_only_the_interval_and_open_pauses() -> None:
    start = T0
    end = T0 + timedelta(hours=1)
    intervals = [
        (T0 - timedelta(minutes=10), T0 + timedelta(minutes=10)),  # 10 min inside
        (T0 + timedelta(minutes=30), T0 + timedelta(minutes=40)),  # 10 min inside
        (T0 + timedelta(minutes=50), None),  # open pause runs to `end`: 10 min
        (T0 + timedelta(hours=2), T0 + timedelta(hours=3)),  # outside
    ]
    assert paused_overlap_seconds(intervals, start, end) == 30 * 60


def test_client_warning_threshold_mirrors_the_rule() -> None:
    assert pace_warn_below_seconds(estimate_seconds=1800, is_first_mark=False, rules=RULES) == 900
    assert pace_warn_below_seconds(estimate_seconds=1800, is_first_mark=True, rules=RULES) is None
    assert pace_warn_below_seconds(estimate_seconds=90, is_first_mark=False, rules=RULES) is None
    assert pace_warn_below_seconds(estimate_seconds=None, is_first_mark=False, rules=RULES) is None


# ---------------------------------------------------------------- GPS

GPS = GpsRules(tolerance_meters=150, min_accuracy_meters=100)
STOPS = [A, B, C]


def _at(stop: StopPoint, *, accuracy: float | None = 20) -> GpsReading:
    assert stop.lat is not None
    assert stop.lng is not None
    return GpsReading(lat=stop.lat, lng=stop.lng, accuracy_m=accuracy)


def test_position_at_the_marked_stop_is_normal() -> None:
    result = gps_verdict(_at(B), stops=STOPS, marked_position=2, rules=GPS)
    assert result.verdict is GpsVerdict.AT
    assert result.distance_bucket_m == 0


def test_marking_earlier_stops_while_further_along_is_normal() -> None:
    result = gps_verdict(_at(C), stops=STOPS, marked_position=1, rules=GPS)
    assert result.verdict is GpsVerdict.BEHIND


def test_marking_a_stop_ahead_is_flagged() -> None:
    result = gps_verdict(_at(A), stops=STOPS, marked_position=3, rules=GPS)
    assert result.verdict is GpsVerdict.AHEAD
    assert result.distance_bucket_m is not None
    assert result.distance_bucket_m % 50 == 0


def test_poor_accuracy_or_no_position_is_unknown() -> None:
    assert (
        gps_verdict(None, stops=STOPS, marked_position=3, rules=GPS).verdict is GpsVerdict.UNKNOWN
    )
    poor = _at(A, accuracy=250)
    assert (
        gps_verdict(poor, stops=STOPS, marked_position=3, rules=GPS).verdict is GpsVerdict.UNKNOWN
    )


def test_position_between_stops_is_unknown() -> None:
    midway = GpsReading(lat=44.505, lng=34.0, accuracy_m=10)  # ~555 m from both
    assert (
        gps_verdict(midway, stops=STOPS, marked_position=3, rules=GPS).verdict is GpsVerdict.UNKNOWN
    )


def test_invalid_coordinates_are_unknown() -> None:
    bogus = GpsReading(lat=123.0, lng=34.0, accuracy_m=10)
    assert (
        gps_verdict(bogus, stops=STOPS, marked_position=1, rules=GPS).verdict is GpsVerdict.UNKNOWN
    )


def test_stops_without_coordinates_are_ignored() -> None:
    stops = [A, StopPoint(2, None, None), C]
    result = gps_verdict(_at(C), stops=stops, marked_position=2, rules=GPS)
    assert result.verdict is GpsVerdict.BEHIND
    assert result.distance_bucket_m is None


# ---------------------------------------------------------------- escalation


def test_batch_window_groups_close_violations() -> None:
    assert not is_batched(None, T0, window_seconds=60)
    assert is_batched(T0, T0 + timedelta(seconds=30), window_seconds=60)
    assert not is_batched(T0, T0 + timedelta(seconds=90), window_seconds=60)


def test_counting_respects_window_and_reset() -> None:
    times = [T0 - timedelta(hours=5), T0 - timedelta(hours=2), T0 - timedelta(minutes=10)]
    assert count_within(times, now=T0, window_hours=3, not_before=None) == 2
    assert count_within(times, now=T0, window_hours=6, not_before=None) == 3
    assert count_within(times, now=T0, window_hours=6, not_before=T0 - timedelta(hours=1)) == 1


def test_flag_at_three_in_three_hours_and_block_at_six_in_six() -> None:
    rules = EscalationRules()
    three = [T0 - timedelta(minutes=m) for m in (5, 40, 90)]
    decision = evaluate_escalation(three, now=T0, counters_from=None, rules=rules)
    assert decision.should_flag
    assert not decision.should_block

    six = [T0 - timedelta(minutes=m) for m in (5, 40, 90, 150, 200, 300)]
    decision = evaluate_escalation(six, now=T0, counters_from=None, rules=rules)
    assert decision.should_flag
    assert decision.should_block

    two = three[:2]
    assert not evaluate_escalation(two, now=T0, counters_from=None, rules=rules).should_flag


def test_counters_reset_after_a_block_or_admin_reset() -> None:
    rules = EscalationRules()
    six = [T0 - timedelta(minutes=m) for m in (5, 40, 90, 150, 200, 300)]
    decision = evaluate_escalation(
        six, now=T0, counters_from=T0 - timedelta(minutes=1), rules=rules
    )
    assert not decision.should_flag


def test_block_ladder_climbs_only_within_the_memory_window() -> None:
    rules = EscalationRules()
    first = next_block_step(ladder_level=0, last_offence_at=None, now=T0, rules=rules)
    assert (first.duration_hours, first.new_ladder_level) == (1, 1)

    second = next_block_step(
        ladder_level=1, last_offence_at=T0 - timedelta(days=2), now=T0, rules=rules
    )
    assert (second.duration_hours, second.new_ladder_level) == (24, 2)

    third = next_block_step(
        ladder_level=2, last_offence_at=T0 - timedelta(days=2), now=T0, rules=rules
    )
    assert third.duration_hours == 168

    capped = next_block_step(
        ladder_level=9, last_offence_at=T0 - timedelta(days=2), now=T0, rules=rules
    )
    assert capped.duration_hours == 168
    assert capped.new_ladder_level == 3

    forgotten = next_block_step(
        ladder_level=2, last_offence_at=T0 - timedelta(days=45), now=T0, rules=rules
    )
    assert forgotten.duration_hours == 1


# ---------------------------------------------------------------- soft limits


def test_moscow_day_starts_at_local_midnight() -> None:
    # 22:30 UTC on 20 Sep is 01:30 on 21 Sep in Moscow.
    now = datetime(2026, 9, 20, 22, 30, tzinfo=UTC)
    start, end = moscow_day_bounds(now)
    assert start == datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 21, 21, 0, tzinfo=UTC)


def test_daily_cap_awards_the_remainder() -> None:
    assert apply_daily_cap(points=300, awarded_today=0, cap=600) == 300
    assert apply_daily_cap(points=300, awarded_today=450, cap=600) == 150
    assert apply_daily_cap(points=300, awarded_today=600, cap=600) == 0
    assert apply_daily_cap(points=300, awarded_today=900, cap=600) == 0
    assert apply_daily_cap(points=0, awarded_today=0, cap=600) == 0


def test_route_cooldown() -> None:
    assert cooldown_active(
        last_awarded_completed_at=T0 - timedelta(days=3), now=T0, cooldown_days=14
    )
    assert not cooldown_active(
        last_awarded_completed_at=T0 - timedelta(days=15), now=T0, cooldown_days=14
    )
    assert not cooldown_active(last_awarded_completed_at=None, now=T0, cooldown_days=14)
    assert not cooldown_active(
        last_awarded_completed_at=T0 - timedelta(days=1), now=T0, cooldown_days=0
    )
