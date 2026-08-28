"""Deterministic safety/quality gate for normalized routing results.

The gate does not claim to certify a hiking trail.  It catches contradictions
that are already visible in the provider response and records what still needs
independent OSM/editorial verification.  That distinction is important for
Crimean mountain routes: a path in a road graph is not proof that it is open,
safe in current weather, or suitable for a particular traveller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from tourism_backend.modules.route_builder.application.place_picker import PickedPlace
from tourism_backend.modules.route_builder.application.routing import (
    RoutingResult,
    TransportMode,
)

RouteQualityStatus = Literal[
    "unverified",
    "verified",
    "verified_with_warnings",
    "needs_review",
    "unusable",
]

_PACE_MAX_ANGLE_DEGREES: dict[str, float] = {
    "calm": 12,
    "moderate": 20,
    "active": 30,
}
_PACE_MAX_GAIN_METERS: dict[str, int] = {
    "calm": 400,
    "moderate": 800,
    "active": 1_600,
}


@dataclass(frozen=True, slots=True)
class RouteQualityAssessment:
    """A versioned, machine-readable result suitable for API metadata."""

    status: RouteQualityStatus
    warnings: tuple[str, ...]
    policy_version: str = "v1"

    @property
    def usable_for_private_draft(self) -> bool:
        return self.status != "unusable"


@dataclass(frozen=True, slots=True)
class RoadEventSignal:
    """Provider-neutral road-event projection consumed by the quality gate.

    The policy receives this small projection rather than an ORM model so
    event descriptions, URLs and other untrusted payload are never copied to
    a route. Events are region-level today; an applicable closure therefore
    stops generation conservatively, while restrictions/congestion require a
    visible review warning.
    """

    status: str
    event_kind: str
    affects_transport: tuple[str, ...] = ()
    starts_at: datetime | None = None
    ends_at: datetime | None = None


def _truthy(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.casefold().strip() in {
            "1",
            "true",
            "yes",
            "required",
            "mandatory",
            "опасно",
        }
    return False


def _accessibility_flags(value: object) -> set[str]:
    flags: set[str] = set()
    if not isinstance(value, dict):
        return flags
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        key_cf = key.casefold().strip().replace("-", "_").replace(" ", "_")
        if _truthy(raw):
            flags.add(key_cf)
        if isinstance(raw, dict):
            flags.update(_accessibility_flags(raw))
    return flags


def _independent_stop_findings(
    stops: Sequence[PickedPlace],
    *,
    season: str | None,
    with_children: bool | None,
    with_pets: bool | None,
) -> tuple[list[str], list[str], list[str]]:
    """Return hard failures, review reasons and bounded data warnings.

    These checks use only first-party/editorial fields already stored on a
    place. Missing evidence is a warning; it is never converted into a claim
    that a trail, bridge or opening hour is safe.
    """

    hard_failures: list[str] = []
    review_reasons: list[str] = []
    warnings: list[str] = []
    for stop in stops:
        closure = (stop.temporary_closure_status or "").casefold().strip()
        if closure in {"closed", "temporarily_closed", "closed_permanently"}:
            hard_failures.append("stop_temporarily_closed")
        elif closure in {"partial", "scheduled", "restricted"}:
            review_reasons.append("stop_access_restriction_requires_review")

        if with_children is True and stop.suitable_for_children is False:
            hard_failures.append("stop_not_suitable_for_children")
        if with_pets is True and stop.suitable_for_pets is False:
            hard_failures.append("stop_not_suitable_for_pets")

        if stop.safety_warnings:
            review_reasons.append("stop_safety_warning_requires_review")

        surface = (stop.surface or "").casefold().strip()
        if surface in {"dirt", "dirt_road", "gravel", "sand", "rough", "offroad"}:
            review_reasons.append("stop_surface_requires_review")

        if season:
            seasons = {item.casefold().strip() for item in stop.seasonality if item.strip()}
            if seasons and season.casefold() not in seasons:
                review_reasons.append("stop_seasonality_mismatch")
            elif not seasons:
                warnings.append("stop_seasonality_unknown")

        flags = _accessibility_flags(stop.accessibility)
        if flags & {"requires_boat", "boat_required", "vertical_climb", "climbing_required"}:
            hard_failures.append("stop_requires_unsafe_access")
        if flags & {"river_crossing", "water_crossing", "crosses_water", "ferry"}:
            review_reasons.append("water_crossing_requires_independent_review")
        if not stop.accessibility:
            warnings.append("stop_accessibility_unknown")

    return hard_failures, review_reasons, warnings


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


_TRANSPORT_ALIASES: dict[str, frozenset[str]] = {
    "walk": frozenset({"walk", "walking", "pedestrian", "foot"}),
    "car": frozenset({"car", "driving", "automobile", "motor_vehicle"}),
    "public": frozenset({"public", "public_transport", "transit"}),
    "mixed": frozenset({"mixed"}),
}


def _event_transport_applies(event: RoadEventSignal, transport_mode: TransportMode) -> bool:
    values = {
        item.casefold().strip().replace("-", "_").replace(" ", "_")
        for item in event.affects_transport
        if isinstance(item, str) and item.strip()
    }
    if not values or values & {"all", "any", "*", "road", "roads"}:
        return True
    aliases = _TRANSPORT_ALIASES.get(transport_mode, frozenset({transport_mode}))
    return bool(values & aliases)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _event_state(event: RoadEventSignal, *, as_of: datetime) -> str:
    """Return ``inactive``, ``active`` or ``scheduled`` for one event."""

    status = event.status.casefold().strip()
    if status == "resolved":
        return "inactive"
    now = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    starts = _aware(event.starts_at)
    ends = _aware(event.ends_at)
    if ends is not None and ends < now:
        return "inactive"
    if status == "scheduled" or (starts is not None and starts > now):
        return "scheduled"
    if status == "active" or starts is None or starts <= now:
        return "active"
    return "inactive"


def _road_event_findings(
    events: Sequence[RoadEventSignal],
    *,
    transport_mode: TransportMode,
    as_of: datetime,
) -> tuple[list[str], list[str], list[str]]:
    """Project applicable road events into hard/review/warning codes.

    A road event has no segment geometry in the current schema, so a matching
    region event cannot be claimed to affect a particular leg. We still fail
    an active closure (the safe default) and mark less certain event kinds for
    editorial/provider review. Unknown event kinds/statuses are never silently
    treated as safe.
    """

    hard: list[str] = []
    review: list[str] = []
    warnings: list[str] = []
    for event in events:
        if not _event_transport_applies(event, transport_mode):
            continue
        state = _event_state(event, as_of=as_of)
        if state == "inactive":
            continue
        kind = event.event_kind.casefold().strip()
        if state == "scheduled":
            if kind == "closure":
                review.append("road_event_scheduled_closure")
            elif kind == "restriction":
                review.append("road_event_scheduled_restriction")
            elif kind == "congestion":
                review.append("road_event_scheduled_congestion")
            else:
                review.append("road_event_scheduled_unknown")
            continue
        if kind == "closure":
            hard.append("road_event_active_closure")
        elif kind == "restriction":
            review.append("road_event_active_restriction")
        elif kind == "congestion":
            review.append("road_event_active_congestion")
        else:
            review.append("road_event_active_unknown")
    return hard, review, warnings


def active_road_event_blockers(
    events: Sequence[RoadEventSignal],
    *,
    transport_mode: TransportMode,
    as_of: datetime | None = None,
) -> tuple[str, ...]:
    """Return only hard road-event blockers for an already stored route.

    Generation uses the full assessment above. Execution can happen hours or
    days later, so it re-checks current region events without re-calling the
    routing provider; this helper keeps that second gate identical to the
    generation policy.
    """

    hard, _review, _warnings = _road_event_findings(
        events,
        transport_mode=transport_mode,
        as_of=as_of or datetime.now(UTC),
    )
    return _dedupe(hard)


def assess_route_quality(
    routing: RoutingResult,
    *,
    transport_mode: TransportMode,
    pace: str | None = None,
    stops: Sequence[PickedPlace] = (),
    season: str | None = None,
    with_children: bool | None = None,
    with_pets: bool | None = None,
    road_events: Sequence[RoadEventSignal] = (),
    as_of: datetime | None = None,
) -> RouteQualityAssessment:
    """Evaluate the data we have without inventing missing safety facts.

    ``verified`` is intentionally not emitted by v1. Independent checks for
    access, water crossings, seasonal closures and trail surface are not yet
    wired into this pure provider-result gate. A sound provider route therefore
    tops out at ``verified_with_warnings``.
    """

    context_hard_failures, context_review_reasons, context_warnings = _independent_stop_findings(
        stops,
        season=season,
        with_children=with_children,
        with_pets=with_pets,
    )
    event_hard, event_review, event_warnings = _road_event_findings(
        road_events,
        transport_mode=transport_mode,
        as_of=as_of or datetime.now(UTC),
    )
    context_hard_failures.extend(event_hard)
    context_review_reasons.extend(event_review)
    context_warnings.extend(event_warnings)
    warnings = [*routing.warnings, *context_warnings]

    if routing.synthetic:
        warnings.extend(("synthetic_geometry", "not_navigation_grade"))
        if context_hard_failures:
            warnings.extend(context_hard_failures)
            return RouteQualityAssessment(status="unusable", warnings=_dedupe(warnings))
        warnings.extend(context_review_reasons)
        return RouteQualityAssessment(status="unverified", warnings=_dedupe(warnings))

    hard_failures = list(context_hard_failures)
    review_reasons = list(context_review_reasons)

    if routing.total_distance_meters <= 0:
        hard_failures.append("invalid_route_distance")
    if not routing.legs:
        hard_failures.append("route_legs_missing")
    if routing.geometry_wkt is None:
        hard_failures.append("provider_geometry_missing")

    for leg in routing.legs:
        if leg.from_index < 0 or leg.to_index <= leg.from_index:
            hard_failures.append("invalid_route_leg_order")
        if leg.distance_meters < 0 or leg.duration_seconds < 0:
            hard_failures.append("invalid_route_leg_metrics")

    road_types = {item.casefold() for item in routing.road_types}
    if transport_mode == "walk" and "highway" in road_types:
        # A highway returned despite the requested filter is incompatible with
        # the pedestrian promise until an editor/provider fixture proves an
        # actual segregated footpath.
        hard_failures.append("pedestrian_highway_filter_violated")
    if "ferry" in road_types:
        review_reasons.append("ferry_schedule_and_access_unknown")
    if "dirt_road" in road_types:
        review_reasons.append("dirt_road_surface_requires_review")
    if transport_mode == "walk" and road_types & {"stairs", "stairway", "ban_stairway"}:
        review_reasons.append("stairs_require_review")

    angle = routing.max_road_angle_degrees
    if angle is not None:
        if transport_mode == "walk" and angle > 45:
            hard_failures.append("extreme_slope")
        elif transport_mode == "car" and angle > 35:
            review_reasons.append("road_slope_requires_review")
        elif transport_mode == "walk":
            requested_limit = _PACE_MAX_ANGLE_DEGREES.get(pace or "")
            if requested_limit is not None and angle > requested_limit:
                review_reasons.append("slope_above_requested_pace")
    elif transport_mode == "walk":
        warnings.append("slope_unknown")

    gain = routing.elevation_gain_meters
    gain_limit = _PACE_MAX_GAIN_METERS.get(pace or "")
    if transport_mode == "walk" and gain_limit is not None:
        if gain is None:
            warnings.append("elevation_gain_unknown")
        elif gain > gain_limit:
            review_reasons.append("elevation_gain_above_requested_pace")

    if hard_failures:
        warnings.extend(hard_failures)
        return RouteQualityAssessment(status="unusable", warnings=_dedupe(warnings))

    # These require independent data sources or editorial evidence. The route
    # may still be useful as a private draft, but the UI must not call it a
    # certified/safe hiking route yet.
    warnings.append("terrain_access_not_independently_verified")
    if review_reasons:
        warnings.extend(review_reasons)
        return RouteQualityAssessment(status="needs_review", warnings=_dedupe(warnings))
    return RouteQualityAssessment(
        status="verified_with_warnings",
        warnings=_dedupe(warnings),
    )
