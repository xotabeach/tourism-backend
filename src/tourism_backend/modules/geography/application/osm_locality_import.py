"""Pure normalization for OpenStreetMap locality candidates.

The Crimea bbox is deliberately only a download boundary. New records remain
inactive until an editor confirms that they belong to the product region and
that names/types are suitable for users.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass, replace
from typing import Any

from tourism_backend.modules.places.application.osm_import import (
    CRIMEA_CANDIDATE_BBOX,
    BoundingBox,
)

OSM_LOCALITY_SOURCE_NAME = "openstreetmap"
OSM_LOCALITY_SOURCE_LICENSE = "ODbL-1.0"
# Physical Crimea boundary plus the separately mapped Sevastopol area. These
# are source identifiers, not a statement about national affiliation. Keeping
# them explicit avoids pulling Kherson and Taman localities from the broad
# validation bbox.
CRIMEA_OSM_REGION_RELATION_IDS = (3_788_824, 1_574_364)
OSM_LOCALITY_TYPES = frozenset(
    {
        "city",
        "town",
        "village",
        "hamlet",
        "isolated_dwelling",
        "suburb",
        "neighbourhood",
    }
)

_PRODUCT_TYPES = {
    "city": "city",
    "town": "town",
    "village": "village",
    "hamlet": "hamlet",
    "isolated_dwelling": "hamlet",
    "suburb": "suburb",
    "neighbourhood": "neighbourhood",
}
_SAME_NAME_RADIUS_METERS = {
    "city": 15_000,
    "town": 8_000,
    "village": 4_000,
    "hamlet": 2_000,
    "suburb": 2_000,
    "neighbourhood": 1_500,
}
_OSM_TYPE_PRIORITY = {"node": 0, "relation": 1, "way": 2}


@dataclass(frozen=True, slots=True)
class OsmLocalityCandidate:
    source_external_id: str
    osm_type: str
    osm_id: int
    name: str
    locality_type: str
    aliases: tuple[str, ...]
    population: int | None
    lat: float
    lng: float
    source_url: str
    source_payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OsmLocalityNormalizationResult:
    candidates: tuple[OsmLocalityCandidate, ...]
    rejected: dict[str, int]
    input_count: int


def build_locality_overpass_query(
    *,
    region_relation_ids: tuple[int, ...] = CRIMEA_OSM_REGION_RELATION_IDS,
) -> str:
    if not region_relation_ids or any(value <= 0 for value in region_relation_ids):
        raise ValueError("at least one positive region relation id is required")
    values = "|".join(sorted(OSM_LOCALITY_TYPES))
    areas = "\n".join(
        f"area(id:{3_600_000_000 + relation_id})->.region{index};"
        for index, relation_id in enumerate(region_relation_ids)
    )
    statements = "\n".join(
        f'  nwr["place"~"^({values})$"](area.region{index});'
        for index in range(len(region_relation_ids))
    )
    return f"""[out:json][timeout:120];
{areas}
(
{statements}
);
out center tags qt;"""


def _coordinates(element: dict[str, Any]) -> tuple[float, float] | None:
    lat = element.get("lat")
    lng = element.get("lon")
    if lat is None or lng is None:
        center = element.get("center")
        if not isinstance(center, dict):
            return None
        lat = center.get("lat")
        lng = center.get("lon")
    if lat is None or lng is None:
        return None
    try:
        result = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= result[0] <= 90 and -180 <= result[1] <= 180):
        return None
    return result


def _fold(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def _split_aliases(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace("|", ";").split(";") if item.strip()]


def _aliases(tags: dict[str, str], name: str) -> tuple[str, ...]:
    values: list[str] = []
    for key in (
        "name",
        "name:ru",
        "official_name",
        "official_name:ru",
        "short_name",
        "alt_name",
        "old_name",
    ):
        values.extend(_split_aliases(tags.get(key)))
    seen = {_fold(name)}
    result: list[str] = []
    for value in values:
        folded = _fold(value)
        if not folded or folded in seen or len(value) > 255:
            continue
        seen.add(folded)
        result.append(value)
    return tuple(result[:16])


def _population(value: str | None) -> int | None:
    if not value:
        return None
    compact = value.replace(" ", "").replace("\u00a0", "").replace(",", "")
    if not compact.isdigit():
        return None
    result = int(compact)
    return result if 0 <= result <= 20_000_000 else None


def _haversine_m(a: OsmLocalityCandidate, b: OsmLocalityCandidate) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _wikidata(candidate: OsmLocalityCandidate) -> str | None:
    tags = candidate.source_payload.get("tags")
    if not isinstance(tags, dict):
        return None
    value = tags.get("wikidata")
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _same_real_locality(a: OsmLocalityCandidate, b: OsmLocalityCandidate) -> bool:
    if a.locality_type != b.locality_type or _fold(a.name) != _fold(b.name):
        return False
    a_wikidata, b_wikidata = _wikidata(a), _wikidata(b)
    if a_wikidata and b_wikidata:
        return a_wikidata == b_wikidata
    return _haversine_m(a, b) <= _SAME_NAME_RADIUS_METERS[a.locality_type]


def _merged_duplicate(
    current: OsmLocalityCandidate,
    incoming: OsmLocalityCandidate,
) -> OsmLocalityCandidate:
    preferred, other = sorted(
        (current, incoming),
        key=lambda item: (
            _OSM_TYPE_PRIORITY[item.osm_type],
            -(item.population or 0),
            item.source_external_id,
        ),
    )
    aliases: list[str] = []
    seen = {_fold(preferred.name)}
    for value in (*preferred.aliases, other.name, *other.aliases):
        folded = _fold(value)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        aliases.append(value)
    merged_payload = dict(preferred.source_payload)
    merged_payload["duplicate_osm_ids"] = sorted(
        {
            current.source_external_id,
            incoming.source_external_id,
            *(str(value) for value in current.source_payload.get("duplicate_osm_ids", [])),
            *(str(value) for value in incoming.source_payload.get("duplicate_osm_ids", [])),
        }
    )
    return replace(
        preferred,
        aliases=tuple(aliases[:16]),
        population=max(current.population or 0, incoming.population or 0) or None,
        source_payload=merged_payload,
    )


def _deduplicate_same_named_localities(
    candidates: list[OsmLocalityCandidate],
) -> tuple[list[OsmLocalityCandidate], int]:
    result: list[OsmLocalityCandidate] = []
    removed = 0
    for candidate in candidates:
        for index, existing in enumerate(result):
            if _same_real_locality(existing, candidate):
                result[index] = _merged_duplicate(existing, candidate)
                removed += 1
                break
        else:
            result.append(candidate)
    return result, removed


def normalize_locality_overpass_payload(
    payload: dict[str, Any],
    *,
    limit: int = 5000,
    bbox: BoundingBox = CRIMEA_CANDIDATE_BBOX,
) -> OsmLocalityNormalizationResult:
    if not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        raise ValueError("Overpass payload must contain an elements array")

    candidates: list[OsmLocalityCandidate] = []
    rejected: Counter[str] = Counter()
    seen: set[str] = set()
    for raw in raw_elements:
        if not isinstance(raw, dict):
            rejected["invalid_element"] += 1
            continue
        osm_type = raw.get("type")
        osm_id = raw.get("id")
        if osm_type not in {"node", "way", "relation"} or not isinstance(osm_id, int):
            rejected["invalid_identity"] += 1
            continue
        source_external_id = f"{osm_type}/{osm_id}"
        if source_external_id in seen:
            rejected["duplicate_identity"] += 1
            continue

        raw_tags = raw.get("tags")
        if not isinstance(raw_tags, dict):
            rejected["missing_tags"] += 1
            continue
        tags = {str(key): str(value) for key, value in raw_tags.items()}
        osm_place = tags.get("place", "").strip().casefold()
        if osm_place not in OSM_LOCALITY_TYPES:
            rejected["unsupported_place_type"] += 1
            continue
        name = (tags.get("name:ru") or tags.get("name") or "").strip()
        if not name or len(name) > 255:
            rejected["missing_or_invalid_name"] += 1
            continue
        coordinates = _coordinates(raw)
        if coordinates is None:
            rejected["missing_coordinates"] += 1
            continue
        lat, lng = coordinates
        if not bbox.contains(lat=lat, lng=lng):
            rejected["outside_candidate_bbox"] += 1
            continue

        seen.add(source_external_id)
        candidates.append(
            OsmLocalityCandidate(
                source_external_id=source_external_id,
                osm_type=osm_type,
                osm_id=osm_id,
                name=name,
                locality_type=_PRODUCT_TYPES[osm_place],
                aliases=_aliases(tags, name),
                population=_population(tags.get("population")),
                lat=lat,
                lng=lng,
                source_url=f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
                source_payload={
                    "type": osm_type,
                    "id": osm_id,
                    "version": raw.get("version"),
                    "tags": tags,
                },
            )
        )

    candidates, duplicate_count = _deduplicate_same_named_localities(candidates)
    if duplicate_count:
        rejected["duplicate_same_name_nearby"] += duplicate_count
    candidates.sort(
        key=lambda item: (
            {"city": 0, "town": 1, "village": 2, "hamlet": 3}.get(item.locality_type, 4),
            -(item.population or 0),
            _fold(item.name),
            item.source_external_id,
        )
    )
    if len(candidates) > limit:
        rejected["not_selected_after_limit"] += len(candidates) - limit
        candidates = candidates[:limit]
    return OsmLocalityNormalizationResult(
        candidates=tuple(candidates),
        rejected=dict(sorted(rejected.items())),
        input_count=len(raw_elements),
    )
