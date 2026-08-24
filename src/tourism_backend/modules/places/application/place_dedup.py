"""Pure decision logic for merging OSM-imported duplicates into seed places.

ADR-009 P0-bis 0b.1: publishing the OSM import wholesale would create visible
catalog duplicates of the 20 seed places (16/20 have an OSM twin within
~400m). `scripts/dedupe_places.py` finds geo candidates in SQL
(`ST_DWithin`), scores names with `name_similarity` below, and hands the
result to the functions here — none of which touch the DB or network, so
they're unit-testable on their own.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

#: `similarity()` alone is fooled by naming conventions like "Х. дворец в
#: Бахчисарае" scoring lower than a typo would; substring containment after
#: normalization catches that case regardless of the trigram score.
_STRIP_CHARS = " .,·-—"


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    place_id: UUID
    name: str
    distance_m: float
    name_similarity: float


def _normalize(name: str) -> str:
    return name.casefold().strip(_STRIP_CHARS)


def name_similarity(a: str, b: str) -> float:
    """0..1 character-level similarity, no DB/network dependency.

    `pg_trgm` would give the same shape of score, but the local dev Postgres
    image can't load it (`pg_trgm.so: undefined symbol:
    pg_mblen_unbounded`); the candidate set here is already narrowed by
    `ST_DWithin` to a handful of rows per seed, so scoring in Python costs
    nothing.
    """
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def matches_candidate(
    seed_name: str, candidate: DuplicateCandidate, *, min_similarity: float
) -> bool:
    """True when a candidate is plausibly the same place as the seed.

    Prefix containment after normalization catches naming conventions like
    "Х. дворец" vs "Х. дворец в Бахчисарае" even when that scores lower on
    `name_similarity` than a genuine typo would. Deliberately prefix-only,
    not substring-anywhere: a plain `a in b` check also matches unrelated
    exhibits *about* the seed place — 'Макет территории "Херсонес
    Таврический"' (a scale model) contains "Херсонес Таврический" as a
    trailing substring but is not the site itself. `name_similarity` is the
    fallback for everything else.
    """
    a, b = _normalize(seed_name), _normalize(candidate.name)
    if a and b and (a.startswith(b) or b.startswith(a)):
        return True
    return candidate.name_similarity >= min_similarity


def group_candidates(
    seed_ids_by_candidate: dict[UUID, set[UUID]],
) -> tuple[dict[UUID, UUID], set[UUID]]:
    """Split OSM candidates into unambiguous 1:1 merges vs. ambiguous ones.

    A candidate that plausibly matches more than one seed place (e.g. a
    generic name like "Генуэзская крепость" whose radius overlapped two
    seeds) is never auto-merged — it is reported so a human decides.
    Returns (merges: candidate_id -> seed_id, ambiguous: set[candidate_id]).
    """
    merges: dict[UUID, UUID] = {}
    ambiguous: set[UUID] = set()
    for candidate_id, seed_ids in seed_ids_by_candidate.items():
        if len(seed_ids) == 1:
            merges[candidate_id] = next(iter(seed_ids))
        elif len(seed_ids) > 1:
            ambiguous.add(candidate_id)
    return merges, ambiguous


#: Typed columns copied from the OSM duplicate onto the seed winner, and
#: only ever filled in when the seed's own value is empty — editorial data
#: always wins, same rule as `promote_osm_fields.py`.
MERGE_FIELDS = ("elevation_meters", "opening_hours_raw", "website_url", "surface")


def fields_to_copy(seed_fields: dict[str, Any], osm_fields: dict[str, Any]) -> dict[str, Any]:
    return {
        field: osm_fields[field]
        for field in MERGE_FIELDS
        if seed_fields.get(field) is None and osm_fields.get(field) is not None
    }


def merged_source_payload(
    seed_payload: dict[str, Any] | None, osm_tags: dict[str, Any], osm_place_id: UUID
) -> dict[str, Any] | None:
    """Fold the OSM copy's wikidata/wikipedia provenance into the seed's
    `source_payload`, for reference only — nothing re-reads this for a seed
    place (importer scripts filter on `source_name == "openstreetmap"`)."""
    provenance = {key: osm_tags[key] for key in ("wikidata", "wikipedia") if osm_tags.get(key)}
    if not provenance:
        return seed_payload
    payload = dict(seed_payload or {})
    merged_from_osm = dict(payload.get("merged_from_osm") or {})
    merged_from_osm[str(osm_place_id)] = provenance
    payload["merged_from_osm"] = merged_from_osm
    return payload
