"""Pure normalization rules for OpenStreetMap place candidates.

The module deliberately does not perform network or database I/O. OSM data is
treated as an untrusted candidate source: normalized records still require the
editorial publication workflow.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

OSM_SOURCE_NAME = "openstreetmap"
OSM_SOURCE_LICENSE = "ODbL-1.0"

OVERPASS_SELECTORS = (
    'nwr["tourism"~"^(attraction|museum|viewpoint|gallery|zoo|theme_park)$"]',
    'nwr["historic"]',
    'nwr["natural"~"^(cave_entrance|peak|waterfall|beach|cliff|spring)$"]',
    'nwr["leisure"~"^(park|garden|nature_reserve|beach_resort)$"]',
    'nwr["amenity"="place_of_worship"]',
    'nwr["craft"="winery"]',
    'nwr["information"="trailhead"]',
)


@dataclass(frozen=True, slots=True)
class BoundingBox:
    south: float
    west: float
    north: float
    east: float

    def contains(self, *, lat: float, lng: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lng <= self.east


# This is a download pre-filter, not an authoritative administrative boundary.
# Every accepted record remains a draft until boundary/editorial validation.
CRIMEA_CANDIDATE_BBOX = BoundingBox(south=44.37, west=32.45, north=46.30, east=36.70)


@dataclass(frozen=True, slots=True)
class OsmPlaceCandidate:
    source_external_id: str
    osm_type: str
    osm_id: int
    name: str
    lat: float
    lng: float
    category_codes: tuple[str, ...]
    payment_status: str
    is_suitable_for_pets: bool | None
    accessibility: dict[str, str] | None
    address: str | None
    source_url: str
    source_payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OsmNormalizationResult:
    candidates: tuple[OsmPlaceCandidate, ...]
    rejected: dict[str, int]
    input_count: int


def build_overpass_query(
    bbox: BoundingBox = CRIMEA_CANDIDATE_BBOX,
    *,
    selectors: tuple[str, ...] = OVERPASS_SELECTORS,
) -> str:
    if not selectors:
        raise ValueError("at least one Overpass selector is required")
    bounds = f"{bbox.south},{bbox.west},{bbox.north},{bbox.east}"
    statements = "\n".join(f"  {selector}({bounds});" for selector in selectors)
    return f"""[out:json][timeout:120];
(
{statements}
);
out center tags qt;"""


def build_overpass_queries(
    bbox: BoundingBox = CRIMEA_CANDIDATE_BBOX,
    *,
    batch_size: int = 1,
) -> tuple[str, ...]:
    if not 1 <= batch_size <= len(OVERPASS_SELECTORS):
        raise ValueError("batch_size is outside the selector range")
    return tuple(
        build_overpass_query(bbox, selectors=OVERPASS_SELECTORS[index : index + batch_size])
        for index in range(0, len(OVERPASS_SELECTORS), batch_size)
    )


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


def _category_codes(tags: dict[str, str]) -> tuple[str, ...]:
    categories: set[str] = set()
    tourism = tags.get("tourism")
    historic = tags.get("historic")
    natural = tags.get("natural")
    leisure = tags.get("leisure")

    if tourism in {"museum", "gallery"}:
        categories.add("museum")
    elif tourism == "viewpoint":
        categories.add("viewpoint")
    elif tourism in {"attraction", "zoo", "theme_park"}:
        categories.add("landmark")

    if historic in {"castle", "fort", "fortification", "citywalls"}:
        categories.add("fortress")
    elif historic in {"palace", "manor"}:
        categories.add("palace")
    elif historic:
        categories.add("monument")

    natural_categories = {
        "cave_entrance": "cave",
        "peak": "mountain",
        "waterfall": "waterfall",
        "beach": "beach",
        "cliff": "nature",
        "spring": "nature",
    }
    if natural in natural_categories:
        categories.add(natural_categories[natural])

    if leisure in {"park", "garden"}:
        categories.add("park")
    elif leisure == "nature_reserve":
        categories.add("nature")
    elif leisure == "beach_resort":
        categories.add("beach")

    if tags.get("amenity") == "place_of_worship":
        categories.add("religious_site")
    if tags.get("craft") == "winery":
        categories.add("winery")
    if tags.get("information") == "trailhead":
        categories.add("trail")

    return tuple(sorted(categories))


def _payment_status(tags: dict[str, str]) -> str:
    value = tags.get("fee", "").strip().lower()
    if value in {"yes", "required"}:
        return "paid"
    if value == "no":
        return "free"
    return "unknown"


def _optional_yes_no(value: str | None) -> bool | None:
    if value in {"yes", "designated", "permissive"}:
        return True
    if value in {"no", "private"}:
        return False
    return None


def _address(tags: dict[str, str]) -> str | None:
    parts = [
        tags.get("addr:street"),
        tags.get("addr:housenumber"),
        tags.get("addr:city") or tags.get("addr:place"),
    ]
    return ", ".join(part.strip() for part in parts if part and part.strip()) or None


def _balanced_selection(
    candidates: list[OsmPlaceCandidate],
    *,
    limit: int,
) -> tuple[list[OsmPlaceCandidate], int]:
    if len(candidates) <= limit:
        return candidates, 0

    buckets: dict[str, list[OsmPlaceCandidate]] = {}
    for candidate in sorted(candidates, key=lambda item: item.source_external_id):
        primary_category = candidate.category_codes[0]
        buckets.setdefault(primary_category, []).append(candidate)

    selected: list[OsmPlaceCandidate] = []
    category_codes = sorted(buckets)
    while len(selected) < limit:
        added = False
        for category_code in category_codes:
            bucket = buckets[category_code]
            if bucket:
                selected.append(bucket.pop(0))
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
    return selected, len(candidates) - len(selected)


def normalize_overpass_payload(
    payload: dict[str, Any],
    *,
    limit: int = 1000,
    bbox: BoundingBox = CRIMEA_CANDIDATE_BBOX,
) -> OsmNormalizationResult:
    if not 1 <= limit <= 5000:
        raise ValueError("limit must be between 1 and 5000")
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, list):
        raise ValueError("Overpass payload must contain an elements array")

    candidates: list[OsmPlaceCandidate] = []
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
        external_id = f"{osm_type}/{osm_id}"
        if external_id in seen:
            rejected["duplicate_identity"] += 1
            continue

        raw_tags = raw.get("tags")
        if not isinstance(raw_tags, dict):
            rejected["missing_tags"] += 1
            continue
        tags = {str(key): str(value) for key, value in raw_tags.items()}
        name = (tags.get("name:ru") or tags.get("name") or tags.get("name:en") or "").strip()
        if not name:
            rejected["missing_name"] += 1
            continue
        coordinates = _coordinates(raw)
        if coordinates is None:
            rejected["missing_coordinates"] += 1
            continue
        lat, lng = coordinates
        if not bbox.contains(lat=lat, lng=lng):
            rejected["outside_candidate_bbox"] += 1
            continue
        category_codes = _category_codes(tags)
        if not category_codes:
            rejected["unsupported_category"] += 1
            continue

        accessibility = None
        if wheelchair := tags.get("wheelchair"):
            accessibility = {"wheelchair": wheelchair, "source": "openstreetmap"}
        source_payload = {
            "type": osm_type,
            "id": osm_id,
            "tags": tags,
        }
        for metadata_key in ("version", "timestamp", "changeset"):
            if metadata_key in raw:
                source_payload[metadata_key] = raw[metadata_key]

        candidates.append(
            OsmPlaceCandidate(
                source_external_id=external_id,
                osm_type=osm_type,
                osm_id=osm_id,
                name=name[:255],
                lat=lat,
                lng=lng,
                category_codes=category_codes,
                payment_status=_payment_status(tags),
                is_suitable_for_pets=_optional_yes_no(tags.get("dog")),
                accessibility=accessibility,
                address=_address(tags),
                source_url=f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
                source_payload=source_payload,
            )
        )
        seen.add(external_id)

    selected, not_selected = _balanced_selection(candidates, limit=limit)
    if not_selected:
        rejected["not_selected_after_limit"] = not_selected
    return OsmNormalizationResult(
        candidates=tuple(selected),
        rejected=dict(sorted(rejected.items())),
        input_count=len(raw_elements),
    )
