"""Promote raw OSM tags into typed `places` columns (ADR-009 P0.3).

`import_osm_crimea.py` stores the full tag dict in `source_payload` but only
maps a handful of them onto columns, so data we already downloaded stayed
invisible to the matching engine: `description` (~16% of places), `ele`
(~15%), `opening_hours`, `website`, `phone`, `surface`.

Pure functions, no I/O — the caller decides what to write and when. Every
value here originates from an untrusted third-party source, so each mapper
validates range/shape and returns None rather than passing junk through.

Visit-duration estimation (P0.5) also lives here: `recommended_visit_minutes`
was 0% filled, which made every generated route's duration count travel time
only, with no time actually spent at the stops.
"""

from __future__ import annotations

import re

#: OSM `ele` is metres above sea level, occasionally with a unit suffix or a
#: comma decimal separator. Crimea's highest point is ~1545 m; the wider bound
#: keeps the parser honest for reuse elsewhere without accepting nonsense.
_MIN_ELEVATION_M = -500
_MAX_ELEVATION_M = 9000
_ELEVATION_PATTERN = re.compile(r"^(-?\d+(?:[.,]\d+)?)\s*(?:m|м)?$", re.IGNORECASE)

_MAX_DESCRIPTION_CHARS = 2000
_MAX_OPENING_HOURS_CHARS = 255
_MAX_SURFACE_CHARS = 32
_MAX_PHONE_CHARS = 64
_MAX_URL_CHARS = 500

#: Typical time actually spent at a place, by category slug. Deliberately
#: coarse: this is a planning estimate that replaces "no estimate at all",
#: not an editorial fact. Editorial values always win (callers fill NULL only).
_VISIT_MINUTES_BY_CATEGORY: dict[str, int] = {
    "museum": 90,
    "fortress": 90,
    "palace": 90,
    "winery": 90,
    "cave": 60,
    "park": 60,
    "nature": 60,
    "beach": 120,
    "mountain": 120,
    "trail": 150,
    "landmark": 40,
    "religious-site": 40,
    "viewpoint": 30,
    "waterfall": 30,
    "monument": 20,
}
_DEFAULT_VISIT_MINUTES = 45


def parse_elevation_meters(raw: str | None) -> int | None:
    """OSM `ele` → integer metres, or None when absent/implausible."""
    if not raw:
        return None
    match = _ELEVATION_PATTERN.match(raw.strip())
    if match is None:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    if not _MIN_ELEVATION_M <= value <= _MAX_ELEVATION_M:
        return None
    return int(round(value))


def _clean_text(raw: str | None, *, max_chars: int) -> str | None:
    if not raw:
        return None
    collapsed = " ".join(raw.split())
    return collapsed[:max_chars] or None


def promoted_description(tags: dict[str, str]) -> str | None:
    """Prefer a Russian description, then the untagged one, then English."""
    for key in ("description:ru", "description", "description:en"):
        value = _clean_text(tags.get(key), max_chars=_MAX_DESCRIPTION_CHARS)
        if value:
            return value
    return None


def promoted_website(tags: dict[str, str]) -> str | None:
    """Only http(s) URLs — OSM `website` also carries bare hosts and junk."""
    for key in ("website", "contact:website", "url"):
        value = _clean_text(tags.get(key), max_chars=_MAX_URL_CHARS)
        if value and (value.startswith("http://") or value.startswith("https://")):
            return value
    return None


def promoted_phone(tags: dict[str, str]) -> str | None:
    for key in ("phone", "contact:phone"):
        value = _clean_text(tags.get(key), max_chars=_MAX_PHONE_CHARS)
        if value:
            return value
    return None


def promoted_opening_hours(tags: dict[str, str]) -> str | None:
    return _clean_text(tags.get("opening_hours"), max_chars=_MAX_OPENING_HOURS_CHARS)


def promoted_surface(tags: dict[str, str]) -> str | None:
    value = _clean_text(tags.get("surface"), max_chars=_MAX_SURFACE_CHARS)
    return value.casefold() if value else None


def estimate_visit_minutes(category_slugs: frozenset[str] | set[str]) -> int:
    """Longest plausible dwell time across a place's categories.

    Max rather than mean on purpose: a spot that is both `mountain` and
    `viewpoint` is visited for the mountain, and under-estimating dwell time
    silently overfills a day plan.
    """
    known = [
        _VISIT_MINUTES_BY_CATEGORY[slug]
        for slug in category_slugs
        if slug in _VISIT_MINUTES_BY_CATEGORY
    ]
    return max(known) if known else _DEFAULT_VISIT_MINUTES
