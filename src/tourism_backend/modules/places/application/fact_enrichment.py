"""Deterministic place fact enrichment from OSM source_payload tags."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_PRICE_RE = re.compile(
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<currency>₽|руб\.?|rub|uah|eur|usd)?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PlaceFactPatch:
    typical_crowding: str | None = None
    price_min_amount: int | None = None
    price_max_amount: int | None = None
    price_currency: str | None = None
    price_notes: str | None = None
    access_transport: tuple[str, ...] | None = None
    parking_available: bool | None = None
    seasonality: tuple[str, ...] | None = None
    recommended_visit_minutes: int | None = None
    payment_status: str | None = None
    is_suitable_for_pets: bool | None = None
    accessibility: dict[str, str] | None = None


def _tags_from_payload(payload: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    tags = payload.get("tags")
    if not isinstance(tags, dict):
        return {}
    return {str(key): str(value) for key, value in tags.items()}


def _yes_no(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"yes", "designated", "permissive", "customers"}:
        return True
    if lowered in {"no", "private", "no_access"}:
        return False
    return None


def _parse_price(tags: dict[str, str]) -> tuple[int | None, int | None, str | None, str | None]:
    raw_candidates = [
        tags.get("charge"),
        tags.get("fee:amount"),
        tags.get("payment:amount"),
        tags.get("fee"),
    ]
    notes_bits: list[str] = []
    amounts: list[int] = []
    currency = "RUB"
    for raw in raw_candidates:
        if not raw or raw.lower() in {"yes", "no", "unknown"}:
            continue
        notes_bits.append(raw)
        for match in _PRICE_RE.finditer(raw):
            amount_raw = match.group("amount").replace(",", ".")
            try:
                amount = int(round(float(amount_raw)))
            except ValueError:
                continue
            amounts.append(amount)
            cur = (match.group("currency") or "").lower()
            if cur in {"eur", "€"}:
                currency = "EUR"
            elif cur in {"usd", "$"}:
                currency = "USD"
            elif cur in {"uah", "грн"}:
                currency = "UAH"
    if not amounts:
        return None, None, None, "; ".join(notes_bits) or None
    return min(amounts), max(amounts), currency, "; ".join(notes_bits) or None


def _access_transport(tags: dict[str, str]) -> tuple[str, ...]:
    modes: set[str] = set()
    foot = _yes_no(tags.get("foot"))
    if foot is True or tags.get("highway") in {"path", "footway", "steps"}:
        modes.add("walk")
    if _yes_no(tags.get("bicycle")) is True:
        modes.add("bike")
    motor = tags.get("motor_vehicle") or tags.get("access")
    if _yes_no(motor) is True or tags.get("highway") in {
        "primary",
        "secondary",
        "tertiary",
        "residential",
    }:
        modes.add("car")
    if tags.get("public_transport") or tags.get("bus") == "yes":
        modes.add("public")
    if tags.get("tourism") == "attraction" and not modes:
        modes.update({"walk", "car"})
    return tuple(sorted(modes))


def _seasonality(tags: dict[str, str]) -> tuple[str, ...]:
    seasonal = (tags.get("seasonal") or "").lower()
    opening = (tags.get("opening_hours:seasonal") or tags.get("season") or "").lower()
    values: set[str] = set()
    blob = f"{seasonal} {opening}"
    mapping = {
        "summer": "лето",
        "лето": "лето",
        "winter": "зима",
        "зима": "зима",
        "spring": "весна",
        "весна": "весна",
        "autumn": "осень",
        "fall": "осень",
        "осень": "осень",
    }
    for key, label in mapping.items():
        if key in blob:
            values.add(label)
    if tags.get("natural") == "beach" or tags.get("tourism") == "beach_resort":
        values.update({"лето", "весна"})
    return tuple(sorted(values))


def _crowding_heuristic(tags: dict[str, str], category_hint: str | None = None) -> str:
    tourism = tags.get("tourism", "")
    natural = tags.get("natural", "")
    if tourism in {"viewpoint", "attraction"} or natural == "beach":
        return "high"
    if tourism in {"museum", "gallery"} or tags.get("historic"):
        return "medium"
    if natural in {"peak", "cave_entrance", "waterfall"}:
        return "low"
    if category_hint in {"beach", "viewpoint"}:
        return "high"
    return "unknown"


def _visit_minutes(tags: dict[str, str]) -> int | None:
    tourism = tags.get("tourism")
    natural = tags.get("natural")
    if tourism == "museum":
        return 90
    if tourism == "viewpoint":
        return 30
    if natural == "beach":
        return 120
    if natural in {"peak", "waterfall", "cave_entrance"}:
        return 60
    if tags.get("historic"):
        return 45
    return None


def facts_from_osm_tags(
    tags: dict[str, str],
    *,
    category_hint: str | None = None,
) -> PlaceFactPatch:
    price_min, price_max, currency, notes = _parse_price(tags)
    fee = (tags.get("fee") or "").lower()
    payment_status = None
    if fee in {"yes", "required"}:
        payment_status = "paid"
    elif fee == "no":
        payment_status = "free"

    accessibility = None
    if wheelchair := tags.get("wheelchair"):
        accessibility = {"wheelchair": wheelchair, "source": "openstreetmap"}

    parking = _yes_no(tags.get("parking"))
    if parking is None and tags.get("amenity") == "parking":
        parking = True

    return PlaceFactPatch(
        typical_crowding=_crowding_heuristic(tags, category_hint),
        price_min_amount=price_min,
        price_max_amount=price_max,
        price_currency=currency,
        price_notes=notes,
        access_transport=_access_transport(tags) or None,
        parking_available=parking,
        seasonality=_seasonality(tags) or None,
        recommended_visit_minutes=_visit_minutes(tags),
        payment_status=payment_status,
        is_suitable_for_pets=_yes_no(tags.get("dog")),
        accessibility=accessibility,
    )


def facts_from_place_payload(
    payload: dict[str, Any] | None,
    *,
    category_hint: str | None = None,
) -> PlaceFactPatch:
    return facts_from_osm_tags(_tags_from_payload(payload), category_hint=category_hint)


def merge_fact_patch(
    *,
    current: dict[str, Any],
    patch: PlaceFactPatch,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Return only fields that should change on the place row."""

    updates: dict[str, Any] = {}

    def _set(field: str, value: Any, *, empty_ok: bool = False) -> None:
        if value is None and not empty_ok:
            return
        existing = current.get(field)
        if overwrite or existing in (None, "", [], "unknown"):
            if field == "typical_crowding" and value == "unknown" and not overwrite:
                return
            updates[field] = value

    _set("typical_crowding", patch.typical_crowding)
    _set("price_min_amount", patch.price_min_amount)
    _set("price_max_amount", patch.price_max_amount)
    if (
        patch.price_currency
        and (overwrite or current.get("price_currency") in (None, "RUB"))
        and (patch.price_min_amount is not None or patch.price_max_amount is not None)
    ):
        updates["price_currency"] = patch.price_currency
    _set("price_notes", patch.price_notes)
    _set("access_transport", list(patch.access_transport) if patch.access_transport else None)
    _set("parking_available", patch.parking_available)
    _set("seasonality", list(patch.seasonality) if patch.seasonality else None)
    _set("recommended_visit_minutes", patch.recommended_visit_minutes)
    _set("payment_status", patch.payment_status)
    _set("is_suitable_for_pets", patch.is_suitable_for_pets)
    if patch.accessibility and (overwrite or not current.get("accessibility")):
        updates["accessibility"] = patch.accessibility
    if updates.get("payment_status") == "paid":
        updates["is_paid"] = True
    elif updates.get("payment_status") == "free":
        updates["is_paid"] = False
    return updates
