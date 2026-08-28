"""Pure 2GIS catalog matching for place enrichment (GIS-06).

The catalog response is untrusted third-party JSON. This module never
publishes a place, never overwrites a published name/location, and never
treats a vendor hit as proof that a trail is safe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from tourism_backend.modules.places.application.place_dedup import name_similarity

MatchDecision = Literal["matched", "ambiguous", "not_found", "skipped"]
MatchConfidence = Literal["high", "medium", "low"]

ALLOWED_APPLY_FIELDS = frozenset(
    {
        "source_checked_at",
        "freshness_status",
        "source_external_id",
        "address",
        "data_quality_status",
    }
)

_MAX_NAME_CHARS = 255
_MAX_ADDRESS_CHARS = 500
_MAX_HOURS_CHARS = 255
_MAX_HITS = 8
_EARTH_RADIUS_M = 6_371_000.0
_HIGH_DISTANCE_M = 80.0
_MEDIUM_DISTANCE_M = 150.0
_REVIEW_DISTANCE_M = 400.0
_HIGH_NAME_SCORE = 0.75
_MEDIUM_NAME_SCORE = 0.55
_PREFIX_NAME_SCORE = 0.86
_STRIP_CHARS = " .,·-—"


@dataclass(frozen=True, slots=True)
class PlaceProbe:
    """The first-party facts we are allowed to send to a catalog search."""

    place_id: UUID
    name: str
    lng: float
    lat: float
    address: str | None = None
    publication_status: str = "draft"
    source_external_id: str | None = None
    data_quality_status: str = "needs_review"
    opening_hours_raw: str | None = None
    source_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CatalogHit:
    provider_id: str
    name: str
    lng: float
    lat: float
    address: str | None = None
    opening_hours: str | None = None
    rubric_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScoredHit:
    hit: CatalogHit
    distance_m: float
    name_score: float
    confidence: MatchConfidence


@dataclass(frozen=True, slots=True)
class EnrichmentVerdict:
    decision: MatchDecision
    confidence: MatchConfidence | None
    hit: CatalogHit | None
    distance_m: float | None
    name_score: float | None
    candidates: int
    reason: str


@dataclass(frozen=True, slots=True)
class PlaceEnrichmentPatch:
    """DB-safe field updates. Empty dict means dry-run had nothing to write."""

    fields: dict[str, Any]
    payload: dict[str, Any]


def haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _clip(value: str | None, max_chars: int) -> str | None:
    if not value:
        return None
    collapsed = " ".join(value.split())
    return collapsed[:max_chars] or None


def _name_score(ours: str, theirs: str) -> float:
    score = name_similarity(ours, theirs)
    left = ours.casefold().strip(_STRIP_CHARS)
    right = theirs.casefold().strip(_STRIP_CHARS)
    if left and right and (left.startswith(right) or right.startswith(left)):
        return max(score, _PREFIX_NAME_SCORE)
    return score


def _confidence(*, distance_m: float, name_score: float) -> MatchConfidence:
    if distance_m <= _HIGH_DISTANCE_M and name_score >= _HIGH_NAME_SCORE:
        return "high"
    if distance_m <= _MEDIUM_DISTANCE_M and name_score >= _MEDIUM_NAME_SCORE:
        return "medium"
    # OSM and 2GIS coordinates often drift on natural features; a unique
    # strong name inside this band is a review candidate, not a miss.
    if distance_m <= _REVIEW_DISTANCE_M and name_score >= _HIGH_NAME_SCORE:
        return "medium"
    return "low"


def already_enriched(payload: object) -> bool:
    """True only after a high-confidence apply wrote a confirmed provider id.

    Ambiguous proposals keep a candidate id with ``applied=false`` so a later
    dry-run can still re-score the place.
    """

    if not isinstance(payload, dict):
        return False
    blob = payload.get("two_gis")
    if not isinstance(blob, dict):
        return False
    provider_id = blob.get("provider_id")
    return (
        isinstance(provider_id, str) and bool(provider_id.strip()) and blob.get("applied") is True
    )


def parse_catalog_items(payload: object) -> tuple[CatalogHit, ...]:
    """Project a 2GIS `/3.0/items` body into bounded hits."""

    if not isinstance(payload, dict):
        return ()
    result = payload.get("result")
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return ()
    hits: list[CatalogHit] = []
    for raw in items[:_MAX_HITS]:
        hit = _parse_item(raw)
        if hit is not None:
            hits.append(hit)
    return tuple(hits)


def _parse_item(raw: object) -> CatalogHit | None:
    if not isinstance(raw, dict):
        return None
    provider_id = raw.get("id")
    name = _clip(raw.get("name") if isinstance(raw.get("name"), str) else None, _MAX_NAME_CHARS)
    if not isinstance(provider_id, (str, int)) or not name:
        return None
    point = raw.get("point")
    if not isinstance(point, dict):
        return None
    lon = point.get("lon")
    lat = point.get("lat")
    if isinstance(lon, bool) or isinstance(lat, bool):
        return None
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return None
    if not -180 <= float(lon) <= 180 or not -90 <= float(lat) <= 90:
        return None
    address = raw.get("full_address_name") or raw.get("address_name")
    rubrics: list[str] = []
    raw_rubrics = raw.get("rubrics")
    if isinstance(raw_rubrics, list):
        for rubric in raw_rubrics[:8]:
            if isinstance(rubric, dict) and isinstance(rubric.get("name"), str):
                clipped = _clip(rubric["name"], 80)
                if clipped:
                    rubrics.append(clipped)
    return CatalogHit(
        provider_id=str(provider_id).strip()[:64],
        name=name,
        lng=float(lon),
        lat=float(lat),
        address=_clip(address if isinstance(address, str) else None, _MAX_ADDRESS_CHARS),
        opening_hours=_schedule_text(raw.get("schedule")),
        rubric_names=tuple(rubrics),
    )


def _schedule_text(value: object) -> str | None:
    if isinstance(value, str):
        return _clip(value, _MAX_HOURS_CHARS)
    if isinstance(value, dict):
        comment = value.get("comment")
        if isinstance(comment, str):
            return _clip(comment, _MAX_HOURS_CHARS)
        # Keep a short, stable fingerprint rather than the whole calendar.
        days = [
            f"{key}:{item.get('working_hours')}"
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, dict) and item.get("working_hours")
        ]
        if days:
            return _clip("; ".join(days[:7]), _MAX_HOURS_CHARS)
    return None


def score_hits(probe: PlaceProbe, hits: tuple[CatalogHit, ...]) -> tuple[ScoredHit, ...]:
    scored: list[ScoredHit] = []
    for hit in hits:
        distance = haversine_m(probe.lng, probe.lat, hit.lng, hit.lat)
        name_score = _name_score(probe.name, hit.name)
        scored.append(
            ScoredHit(
                hit=hit,
                distance_m=round(distance, 1),
                name_score=round(name_score, 3),
                confidence=_confidence(distance_m=distance, name_score=name_score),
            )
        )
    scored.sort(key=lambda item: (item.distance_m, -item.name_score))
    return tuple(scored)


def decide_match(probe: PlaceProbe, hits: tuple[CatalogHit, ...]) -> EnrichmentVerdict:
    if already_enriched(probe.source_payload):
        return EnrichmentVerdict(
            decision="skipped",
            confidence=None,
            hit=None,
            distance_m=None,
            name_score=None,
            candidates=0,
            reason="already_has_two_gis_id",
        )
    scored = score_hits(probe, hits)
    high = [item for item in scored if item.confidence == "high"]
    medium = [item for item in scored if item.confidence == "medium"]
    if len(high) == 1 and not medium:
        winner = high[0]
        return EnrichmentVerdict(
            decision="matched",
            confidence="high",
            hit=winner.hit,
            distance_m=winner.distance_m,
            name_score=winner.name_score,
            candidates=len(scored),
            reason="unique_high_confidence",
        )
    if len(high) > 1 or (high and medium):
        return EnrichmentVerdict(
            decision="ambiguous",
            confidence="medium" if not high else "high",
            hit=high[0].hit if high else medium[0].hit,
            distance_m=(high[0] if high else medium[0]).distance_m,
            name_score=(high[0] if high else medium[0]).name_score,
            candidates=len(scored),
            reason="multiple_nearby_candidates",
        )
    if medium:
        winner = medium[0]
        return EnrichmentVerdict(
            decision="ambiguous",
            confidence="medium",
            hit=winner.hit,
            distance_m=winner.distance_m,
            name_score=winner.name_score,
            candidates=len(scored),
            reason="medium_confidence_needs_review",
        )
    return EnrichmentVerdict(
        decision="not_found",
        confidence=None,
        hit=scored[0].hit if scored else None,
        distance_m=scored[0].distance_m if scored else None,
        name_score=scored[0].name_score if scored else None,
        candidates=len(scored),
        reason="no_confident_catalog_hit",
    )


def sanitized_report_row(probe: PlaceProbe, verdict: EnrichmentVerdict) -> dict[str, Any]:
    """Public report row: no API key, no raw vendor JSON, no extra PII."""

    return {
        "place_id": str(probe.place_id),
        "name": probe.name,
        "decision": verdict.decision,
        "reason": verdict.reason,
        "confidence": verdict.confidence,
        "distance_m": verdict.distance_m,
        "name_score": verdict.name_score,
        "candidates": verdict.candidates,
        "provider_id": verdict.hit.provider_id if verdict.hit else None,
        "provider_name": verdict.hit.name if verdict.hit else None,
    }


def build_apply_patch(
    probe: PlaceProbe,
    verdict: EnrichmentVerdict,
    *,
    fetched_at: datetime | None = None,
) -> PlaceEnrichmentPatch | None:
    """Return writes allowed after a human-reviewed dry-run.

    High-confidence unique matches may fill empty technical fields.
    Ambiguous matches only mark ``data_quality_status=needs_review`` when the
    row is not already editorially reviewed. Published names and coordinates
    are never patched.
    """

    now = fetched_at or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if verdict.decision == "ambiguous":
        if probe.data_quality_status == "editorial_reviewed":
            return None
        payload = dict(probe.source_payload or {})
        payload["two_gis"] = _two_gis_payload(
            probe,
            verdict,
            fetched_at=now,
            applied=False,
            applied_fields=("data_quality_status",),
        )
        return _checked_patch(
            {"data_quality_status": "needs_review"},
            payload,
        )
    if verdict.decision != "matched" or verdict.hit is None or verdict.confidence != "high":
        return None

    fields: dict[str, Any] = {
        "source_checked_at": now,
        "freshness_status": "fresh",
    }
    if not (probe.source_external_id or "").strip():
        fields["source_external_id"] = f"2gis:{verdict.hit.provider_id}"
    if not (probe.address or "").strip() and verdict.hit.address:
        fields["address"] = verdict.hit.address
    payload = dict(probe.source_payload or {})
    payload["two_gis"] = _two_gis_payload(
        probe,
        verdict,
        fetched_at=now,
        applied=True,
        applied_fields=tuple(fields),
    )
    return _checked_patch(fields, payload)


def _checked_patch(fields: dict[str, Any], payload: dict[str, Any]) -> PlaceEnrichmentPatch:
    unexpected = set(fields) - ALLOWED_APPLY_FIELDS
    if unexpected:
        raise ValueError(f"2GIS apply refused unexpected fields: {sorted(unexpected)}")
    return PlaceEnrichmentPatch(fields=fields, payload=payload)


def _two_gis_payload(
    probe: PlaceProbe,
    verdict: EnrichmentVerdict,
    *,
    fetched_at: datetime,
    applied: bool,
    applied_fields: tuple[str, ...],
) -> dict[str, Any]:
    hit = verdict.hit
    kept_existing = [
        name
        for name, present in (
            ("address", bool((probe.address or "").strip())),
            ("source_external_id", bool((probe.source_external_id or "").strip())),
            ("opening_hours_raw", bool((probe.opening_hours_raw or "").strip())),
            ("name", True),
            ("location", True),
            ("publication_status", True),
        )
        if present
    ]
    return {
        "provider_id": hit.provider_id if hit else None,
        "fetched_at": fetched_at.astimezone(UTC).isoformat(),
        "api_version": "3.0",
        "fields": ["name", "address", "point", "schedule", "rubrics"],
        "confidence": verdict.confidence,
        "distance_meters": verdict.distance_m,
        "decision": verdict.decision,
        "applied": applied,
        "name": hit.name if hit else None,
        "address": hit.address if hit else None,
        "opening_hours": hit.opening_hours if hit else None,
        "rubrics": list(hit.rubric_names) if hit else [],
        "proposals": {
            "opening_hours": hit.opening_hours if hit else None,
            "opening_hours_timezone": "UTC",
            "observed_at": fetched_at.astimezone(UTC).isoformat(),
            "name": hit.name if hit else None,
            "lng": hit.lng if hit else None,
            "lat": hit.lat if hit else None,
            "rubrics": list(hit.rubric_names) if hit else [],
        },
        "audit": {
            "actor": "scripts/enrich_places_2gis.py",
            "applied_fields": list(applied_fields),
            "kept_existing": kept_existing,
        },
    }
