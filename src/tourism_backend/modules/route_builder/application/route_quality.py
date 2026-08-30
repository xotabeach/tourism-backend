"""Deterministic safety/quality gate for normalized routing results.

The gate does not claim to certify a hiking trail. It combines the provider
result with first-party/editorial stop fields and a bounded OSM tag
projection. A path in a road graph is not proof that it is open, safe in
current weather, or suitable for a particular traveller — missing evidence
stays a warning, never a silent "safe".
"""

from __future__ import annotations

import math
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
    policy_version: str = "v2"

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


@dataclass(frozen=True, slots=True)
class TerrainFeatureSignal:
    """Independent OSM coastline/trail geometry near a route (see Track D).

    Pre-filtered to the route's bounding box by the caller (PostGIS
    ``ST_DWithin`` — see generate_service.py); this module stays DB-free and
    only does planar point/segment math, same spirit as ``_linestring_points``
    for provider geometry. Crimea's small extent makes the planar
    approximation good enough for a review/warning signal, not a survey.
    """

    kind: Literal["coastline", "trail"]
    points: tuple[tuple[float, float], ...]


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


_FORBIDDEN_ACCESS = frozenset({"no", "private", "military", "forbidden"})
_WATERWAY_REVIEW = frozenset({"river", "canal", "stream", "tidal", "drain", "rapids"})
_FORD_TRUE = frozenset({"yes", "stepping_stones", "ford"})
_HARD_SAC_SCALE = frozenset(
    {
        "demanding_mountain_hiking",
        "alpine_hiking",
        "demanding_alpine_hiking",
        "difficult_alpine_hiking",
    }
)
_IMPASSABLE_SMOOTHNESS = frozenset({"impassable", "very_horrible", "horrible"})
_MOTORWAY_HIGHWAYS = frozenset({"motorway", "motorway_link", "trunk", "trunk_link"})


def _osm_tag(tags: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = tags.get(key)
        if value:
            return value
    return ""


def _independent_osm_findings(
    tags: dict[str, str],
    *,
    transport_mode: TransportMode,
) -> tuple[list[str], list[str], list[str]]:
    """Project allowlisted OSM tags into hard/review/warning codes.

    Tags are observations, not a field survey. ``foot=yes`` on a private
    estate still needs editorial confirmation; unknown tags stay unknown.
    """

    hard: list[str] = []
    review: list[str] = []
    warnings: list[str] = []
    access = _osm_tag(tags, "access")
    foot = _osm_tag(tags, "foot")
    vehicle = _osm_tag(tags, "vehicle", "motor_vehicle", "motorcar")
    if transport_mode == "walk":
        if foot in _FORBIDDEN_ACCESS or (
            access in _FORBIDDEN_ACCESS and foot not in {"yes", "designated"}
        ):
            hard.append("osm_access_forbidden")
    elif transport_mode == "car":
        if vehicle in _FORBIDDEN_ACCESS or (
            access in _FORBIDDEN_ACCESS and vehicle not in {"yes", "destination", "permissive"}
        ):
            hard.append("osm_access_forbidden")
    elif access in _FORBIDDEN_ACCESS:
        review.append("osm_access_requires_review")

    ford = _osm_tag(tags, "ford")
    waterway = _osm_tag(tags, "waterway")
    bridge = _osm_tag(tags, "bridge")
    if ford in _FORD_TRUE or (waterway in _WATERWAY_REVIEW and bridge not in {"yes", "viaduct"}):
        review.append("osm_water_crossing_requires_review")
    if _osm_tag(tags, "boat") in {"yes", "required", "mandatory"}:
        hard.append("stop_requires_unsafe_access")

    sac = _osm_tag(tags, "sac_scale")
    if sac in _HARD_SAC_SCALE:
        review.append("osm_demanding_trail_requires_review")
    elif sac == "mountain_hiking":
        review.append("osm_mountain_hiking_requires_review")

    smoothness = _osm_tag(tags, "smoothness")
    if smoothness in _IMPASSABLE_SMOOTHNESS:
        if transport_mode == "car":
            hard.append("osm_surface_impassable")
        else:
            review.append("osm_surface_requires_review")
    tracktype = _osm_tag(tags, "tracktype")
    if tracktype in {"grade4", "grade5"}:
        review.append("osm_rough_track_requires_review")

    highway = _osm_tag(tags, "highway")
    if transport_mode == "walk" and highway in _MOTORWAY_HIGHWAYS:
        hard.append("osm_pedestrian_motorway")
    if _osm_tag(tags, "barrier") in {"gate", "lift_gate", "block"} and access in _FORBIDDEN_ACCESS:
        review.append("osm_barrier_requires_review")
    return hard, review, warnings


def _independent_stop_findings(
    stops: Sequence[PickedPlace],
    *,
    transport_mode: TransportMode,
    season: str | None,
    with_children: bool | None,
    with_pets: bool | None,
) -> tuple[list[str], list[str], list[str]]:
    """Return hard failures, review reasons and bounded data warnings.

    These checks use first-party/editorial fields and the bounded OSM tag
    projection. Missing evidence is a warning; it is never converted into a
    claim that a trail, bridge or opening hour is safe.
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

        if stop.access_transport:
            allowed = {
                item.casefold().strip().replace("-", "_").replace(" ", "_")
                for item in stop.access_transport
                if item.strip()
            }
            aliases = _TRANSPORT_ALIASES.get(transport_mode, frozenset({transport_mode}))
            if allowed and not (allowed & aliases) and not (allowed & {"all", "any"}):
                review_reasons.append("stop_access_transport_mismatch")

        if stop.osm_tags:
            osm_hard, osm_review, osm_warnings = _independent_osm_findings(
                stop.osm_tags,
                transport_mode=transport_mode,
            )
            hard_failures.extend(osm_hard)
            review_reasons.extend(osm_review)
            warnings.extend(osm_warnings)
        else:
            warnings.append("stop_osm_tags_unknown")

    return hard_failures, review_reasons, warnings


def _linestring_points(wkt: str | None) -> list[tuple[float, float]]:
    """Parse a 2D LINESTRING without importing the provider adapter."""

    if not isinstance(wkt, str):
        return []
    match = wkt.strip()
    if not match.upper().startswith("LINESTRING"):
        return []
    start = match.find("(")
    end = match.rfind(")")
    if start < 0 or end <= start:
        return []
    points: list[tuple[float, float]] = []
    for raw_point in match[start + 1 : end].split(","):
        fields = raw_point.strip().split()
        if len(fields) < 2:
            continue
        try:
            lon = float(fields[0])
            lat = float(fields[1])
        except ValueError:
            continue
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            continue
        points.append((lon, lat))
        if len(points) > 50_000:
            return []
    return points


# Route geometry can carry thousands of points; a terrain feature (coastline
# ways especially) can too. Both are capped before pairwise comparison so a
# pathological input cannot turn an O(segments x features) check into an
# unbounded one — this is a review/warning signal, not a survey, so a
# stride-sampled subset of a very long line is an acceptable trade.
_MAX_ROUTE_POINTS_FOR_TERRAIN_CHECK = 300
_MAX_FEATURE_POINTS_FOR_TERRAIN_CHECK = 300
_TRAIL_PROXIMITY_DEGREES = 0.00045  # roughly 50m at Crimea's latitude


def _stride_sample(
    points: Sequence[tuple[float, float]], max_points: int
) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return list(points)
    step = len(points) / max_points
    return [points[int(i * step)] for i in range(max_points)]


def _segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    """Planar segment intersection test (orientation + on-segment cases)."""

    def cross(o: tuple[float, float], p: tuple[float, float], q: tuple[float, float]) -> float:
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    def on_segment(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(
            p[1], r[1]
        )

    d1 = cross(b1, b2, a1)
    d2 = cross(b1, b2, a2)
    d3 = cross(a1, a2, b1)
    d4 = cross(a1, a2, b2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    if d1 == 0 and on_segment(b1, a1, b2):
        return True
    if d2 == 0 and on_segment(b1, a2, b2):
        return True
    if d3 == 0 and on_segment(a1, b1, a2):
        return True
    return bool(d4 == 0 and on_segment(a1, b2, a2))


def _point_segment_distance(
    point: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    px, py = point
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    proj_x, proj_y = ax + t * dx, ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _independent_terrain_findings(
    route_wkt: str | None,
    *,
    transport_mode: TransportMode,
    road_types: set[str],
    terrain_features: Sequence[TerrainFeatureSignal],
) -> tuple[list[str], list[str], list[str]]:
    """Independent OSM coastline/trail cross-check against route geometry.

    Not a field survey: OSM way coverage is incomplete, so a route with no
    nearby trail stays a bounded warning, not a hard failure. A route that
    crosses a mapped coastline without a ferry leg is a stronger, but still
    review (not unusable) signal — the provider's own geometry could be a
    short bridge/isthmus the coastline extract does not resolve at this
    scale.
    """

    if not terrain_features:
        return [], [], ["route_terrain_features_unavailable"]

    route_points = _stride_sample(
        _linestring_points(route_wkt), _MAX_ROUTE_POINTS_FOR_TERRAIN_CHECK
    )
    if len(route_points) < 2:
        return [], [], []

    hard: list[str] = []
    review: list[str] = []
    warnings: list[str] = []

    coastline = [f for f in terrain_features if f.kind == "coastline"]
    trails = [f for f in terrain_features if f.kind == "trail"]

    if coastline and "ferry" not in road_types:
        crosses = False
        for i in range(len(route_points) - 1):
            a1, a2 = route_points[i], route_points[i + 1]
            for feature in coastline:
                pts = _stride_sample(feature.points, _MAX_FEATURE_POINTS_FOR_TERRAIN_CHECK)
                for j in range(len(pts) - 1):
                    if _segments_intersect(a1, a2, pts[j], pts[j + 1]):
                        crosses = True
                        break
                if crosses:
                    break
            if crosses:
                break
        if crosses:
            review.append("route_crosses_coastline_without_ferry")

    if transport_mode == "walk":
        if trails:
            trail_segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
            for feature in trails:
                sampled = _stride_sample(feature.points, _MAX_FEATURE_POINTS_FOR_TERRAIN_CHECK)
                trail_segments.extend((sampled[j], sampled[j + 1]) for j in range(len(sampled) - 1))
            far_points = sum(
                1
                for point in route_points
                if min(_point_segment_distance(point, a, b) for a, b in trail_segments)
                > _TRAIL_PROXIMITY_DEGREES
            )
            if far_points > len(route_points) // 2:
                warnings.append("route_segment_far_from_known_trail")
        else:
            warnings.append("route_trail_coverage_unknown")

    return hard, review, warnings


def _geometry_topology_findings(
    wkt: str | None,
    *,
    distance_meters: int,
) -> tuple[list[str], list[str], list[str]]:
    """Catch empty or two-point geometry that looks like a straight line."""

    hard: list[str] = []
    review: list[str] = []
    warnings: list[str] = []
    if wkt is None:
        return hard, review, warnings
    points = _linestring_points(wkt)
    if len(points) < 2:
        hard.append("provider_geometry_unparseable")
        return hard, review, warnings
    # Short hops can be two vertices. A long "detailed" line with two points
    # is the stub/straight-line failure mode, not a road graph.
    if len(points) == 2 and distance_meters > 250:
        review.append("geometry_looks_like_straight_line")
    return hard, review, warnings


def _has_independent_stop_evidence(stops: Sequence[PickedPlace]) -> bool:
    return any(
        stop.osm_tags
        or stop.accessibility
        or stop.surface
        or stop.safety_warnings
        or stop.temporary_closure_status
        or stop.seasonality
        or stop.access_transport
        for stop in stops
    )


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
    terrain_features: Sequence[TerrainFeatureSignal] = (),
    as_of: datetime | None = None,
) -> RouteQualityAssessment:
    """Evaluate the data we have without inventing missing safety facts.

    ``verified`` is still not emitted: OSM tags and editorial fields are
    independent of the provider graph, but they are not a field survey.
    A sound provider route with independent stop checks therefore tops out
    at ``verified_with_warnings``. Missing independent evidence stays an
    explicit warning rather than a silent pass.
    """

    context_hard_failures, context_review_reasons, context_warnings = _independent_stop_findings(
        stops,
        transport_mode=transport_mode,
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
    else:
        geo_hard, geo_review, geo_warnings = _geometry_topology_findings(
            routing.geometry_wkt,
            distance_meters=routing.total_distance_meters,
        )
        hard_failures.extend(geo_hard)
        review_reasons.extend(geo_review)
        warnings.extend(geo_warnings)

    for leg in routing.legs:
        if leg.from_index < 0 or leg.to_index <= leg.from_index:
            hard_failures.append("invalid_route_leg_order")
        if leg.distance_meters < 0 or leg.duration_seconds < 0:
            hard_failures.append("invalid_route_leg_metrics")

    road_types = {item.casefold() for item in routing.road_types}
    if transport_mode == "walk" and "highway" in road_types:
        # 2GIS may still return an excluded road type when it cannot build a
        # path without it. That is not a certified sidewalk, but treating it
        # as unusable would reject ordinary Crimean coastal walks. Editorial
        # review is the honest status until an independent footpath check.
        review_reasons.append("pedestrian_highway_filter_violated")
    if "ferry" in road_types:
        review_reasons.append("ferry_schedule_and_access_unknown")
    if "dirt_road" in road_types:
        review_reasons.append("dirt_road_surface_requires_review")
    if transport_mode == "walk" and road_types & {"stairs", "stairway", "ban_stairway"}:
        review_reasons.append("stairs_require_review")

    terrain_hard, terrain_review, terrain_warnings = _independent_terrain_findings(
        routing.geometry_wkt,
        transport_mode=transport_mode,
        road_types=road_types,
        terrain_features=terrain_features,
    )
    hard_failures.extend(terrain_hard)
    review_reasons.extend(terrain_review)
    warnings.extend(terrain_warnings)

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

    if not _has_independent_stop_evidence(stops):
        warnings.append("terrain_access_not_independently_verified")
    if review_reasons:
        warnings.extend(review_reasons)
        return RouteQualityAssessment(status="needs_review", warnings=_dedupe(warnings))
    return RouteQualityAssessment(
        status="verified_with_warnings",
        warnings=_dedupe(warnings),
    )
