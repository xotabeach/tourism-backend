"""Quality-approved place DTOs for the planning LLM (AI-01).

The model never receives unpublished, closed, rejected, or OSM-forbidden
stops, and never gets raw ORM / source_payload. Freshness is advisory DATA.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from tourism_backend.modules.places.application.osm_field_promotion import (
    safety_tags_from_payload,
)

CANDIDATE_DTO_KEYS = frozenset(
    {
        "place_id",
        "title",
        "subtitle",
        "freshness_status",
        "data_quality_status",
    }
)
DETAIL_DTO_KEYS = CANDIDATE_DTO_KEYS | frozenset(
    {
        "is_paid",
        "price_notes",
        "visit_minutes",
        "children_ok",
        "pets_ok",
        "access_transport",
        "seasonality",
        "crowding",
    }
)
_CLOSED = frozenset({"closed", "temporarily_closed", "closed_permanently", "partial"})
_REJECTED_QUALITY = frozenset({"rejected"})
_ALLOWED_FRESHNESS = frozenset({"unknown", "fresh", "stale", "expired"})
_ALLOWED_DATA_QUALITY = frozenset({"needs_review", "auto_validated", "editorial_reviewed"})
_FORBIDDEN_ACCESS = frozenset({"no", "private", "military", "forbidden"})


def _as_uuid_str(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    return str(value).strip()


def _freshness_status(raw: object) -> str:
    if isinstance(raw, str) and raw in _ALLOWED_FRESHNESS:
        return raw
    return "unknown"


def _data_quality_status(raw: object) -> str:
    if isinstance(raw, str) and raw in _ALLOWED_DATA_QUALITY:
        return raw
    return "needs_review"


def osm_access_forbidden(
    tags: Mapping[str, str] | None,
    *,
    transport_mode: str,
) -> bool:
    """True when allowlisted OSM tags say the stop is not traversable."""

    if not tags:
        return False
    access = (tags.get("access") or "").casefold()
    foot = (tags.get("foot") or "").casefold()
    vehicle = (
        tags.get("vehicle") or tags.get("motor_vehicle") or tags.get("motorcar") or ""
    ).casefold()
    mode = transport_mode.casefold().strip()
    if mode in {"walk", "walking", "pedestrian"}:
        return foot in _FORBIDDEN_ACCESS or (
            access in _FORBIDDEN_ACCESS and foot not in {"yes", "designated"}
        )
    if mode in {"car", "driving", "auto"}:
        return vehicle in _FORBIDDEN_ACCESS or (
            access in _FORBIDDEN_ACCESS and vehicle not in {"yes", "destination", "permissive"}
        )
    return access in _FORBIDDEN_ACCESS


def is_ai_approved_place(place: Any, *, constraints: Mapping[str, Any]) -> bool:
    """Hard gate shared by search / details / near-point tools."""

    publication = getattr(place, "publication_status", None)
    if publication != "published":
        return False
    if getattr(place, "merged_into_place_id", None) is not None:
        return False
    if getattr(place, "data_quality_status", None) in _REJECTED_QUALITY:
        return False
    closure = getattr(place, "temporary_closure_status", None)
    if isinstance(closure, str) and closure in _CLOSED:
        return False
    if (
        constraints.get("with_children") is True
        and getattr(place, "is_suitable_for_children", None) is False
    ):
        return False
    if (
        constraints.get("with_pets") is True
        and getattr(place, "is_suitable_for_pets", None) is False
    ):
        return False
    payload = getattr(place, "source_payload", None)
    tags = safety_tags_from_payload(payload)
    transport = str(constraints.get("transport_mode") or "walk")
    return not osm_access_forbidden(tags, transport_mode=transport)


def candidate_dto(place: Any) -> dict[str, str | None]:
    """Bounded card for the model and place chips. Extra ORM fields stay out."""

    subtitle_raw = getattr(place, "short_description", None)
    subtitle = (str(subtitle_raw)[:120] if subtitle_raw else "") or None
    return {
        "place_id": _as_uuid_str(place.id),
        "title": str(place.name or "")[:80],
        "subtitle": subtitle,
        "freshness_status": _freshness_status(place.freshness_status),
        "data_quality_status": _data_quality_status(place.data_quality_status),
    }


def detail_dto(place: Any) -> dict[str, Any]:
    """Allowlisted facts for get_place_details. No coords, hours, or payload."""

    card = candidate_dto(place)
    crowding = getattr(place, "typical_crowding", None)
    crowding_out = crowding if isinstance(crowding, str) and crowding[:16] else None
    transport = list(getattr(place, "access_transport", None) or [])[:4]
    seasonality = list(getattr(place, "seasonality", None) or [])[:4]
    price_notes = getattr(place, "price_notes", None)
    return {
        **card,
        "is_paid": bool(getattr(place, "is_paid", False)),
        "price_notes": (str(price_notes)[:160] if price_notes else None),
        "visit_minutes": getattr(place, "recommended_visit_minutes", None),
        "children_ok": getattr(place, "is_suitable_for_children", None),
        "pets_ok": getattr(place, "is_suitable_for_pets", None),
        "access_transport": [str(item)[:32] for item in transport if item],
        "seasonality": [str(item)[:32] for item in seasonality if item],
        "crowding": crowding_out,
    }
