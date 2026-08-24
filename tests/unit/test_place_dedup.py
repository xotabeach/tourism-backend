"""Unit tests for OSM/seed place merge decision logic (ADR-009 P0-bis 0b.1)."""

from __future__ import annotations

from uuid import uuid4

from tourism_backend.modules.places.application.place_dedup import (
    DuplicateCandidate,
    fields_to_copy,
    group_candidates,
    matches_candidate,
    merged_source_payload,
)


def _candidate(
    name: str, *, similarity: float = 0.0, distance_m: float = 100.0
) -> DuplicateCandidate:
    return DuplicateCandidate(
        place_id=uuid4(), name=name, distance_m=distance_m, name_similarity=similarity
    )


def test_prefix_containment_matches_regardless_of_similarity() -> None:
    candidate = _candidate("Ханский дворец в Бахчисарае", similarity=0.1)
    assert matches_candidate("Ханский дворец", candidate, min_similarity=0.5) is True


def test_low_similarity_unrelated_name_does_not_match() -> None:
    candidate = _candidate("Воронцовский дворец", similarity=0.1)
    assert matches_candidate("Ханский дворец", candidate, min_similarity=0.5) is False


def test_seed_name_as_trailing_substring_does_not_auto_match() -> None:
    """A scale-model exhibit named after the site is not the site itself —
    only a *prefix* relationship should short-circuit the similarity check."""
    candidate = _candidate('Макет территории "Херсонес Таврический"', similarity=0.68)
    assert matches_candidate("Херсонес Таврический", candidate, min_similarity=0.75) is False


def test_high_similarity_typo_matches() -> None:
    candidate = _candidate("Ханский дворц", similarity=0.6)
    assert matches_candidate("Ханский дворец", candidate, min_similarity=0.5) is True


def test_group_candidates_splits_unambiguous_from_ambiguous() -> None:
    seed_a, seed_b = uuid4(), uuid4()
    clean_candidate, contested_candidate = uuid4(), uuid4()
    merges, ambiguous = group_candidates(
        {
            clean_candidate: {seed_a},
            contested_candidate: {seed_a, seed_b},
        }
    )
    assert merges == {clean_candidate: seed_a}
    assert ambiguous == {contested_candidate}


def test_fields_to_copy_only_fills_empty_seed_fields() -> None:
    seed_fields = {
        "elevation_meters": None,
        "opening_hours_raw": "Mo-Su 09:00-18:00",
        "website_url": None,
        "surface": None,
    }
    osm_fields = {
        "elevation_meters": 250,
        "opening_hours_raw": "Mo-Su 08:00-20:00",
        "website_url": "https://example.org",
        "surface": None,
    }
    assert fields_to_copy(seed_fields, osm_fields) == {
        "elevation_meters": 250,
        "website_url": "https://example.org",
    }


def test_merged_source_payload_records_provenance_per_osm_place() -> None:
    osm_id = uuid4()
    payload = merged_source_payload(None, {"wikidata": "Q123", "wikipedia": "ru:X"}, osm_id)
    assert payload == {"merged_from_osm": {str(osm_id): {"wikidata": "Q123", "wikipedia": "ru:X"}}}


def test_merged_source_payload_without_provenance_keeps_seed_payload_unchanged() -> None:
    seed_payload = {"tags": {"amenity": "castle"}}
    assert merged_source_payload(seed_payload, {}, uuid4()) is seed_payload
