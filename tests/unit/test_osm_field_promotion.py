"""Unit tests for promoting OSM tags into typed place columns (ADR-009 P0.3)."""

from __future__ import annotations

import pytest

from tourism_backend.modules.places.application.osm_field_promotion import (
    estimate_visit_minutes,
    parse_elevation_meters,
    promoted_description,
    promoted_opening_hours,
    promoted_phone,
    promoted_surface,
    promoted_website,
    safety_tags_from_payload,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234", 1234),
        ("1234 m", 1234),
        ("1545", 1545),
        ("1234.6", 1235),
        ("1234,6", 1235),
        ("  760  ", 760),
        ("-20", -20),
        ("120 м", 120),
    ],
)
def test_parse_elevation_accepts_real_osm_shapes(raw: str, expected: int) -> None:
    assert parse_elevation_meters(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "about 300", "300ft", "1e9", "99999", "-9999", "3,4,5", "high"],
)
def test_parse_elevation_rejects_junk(raw: str | None) -> None:
    assert parse_elevation_meters(raw) is None


def test_description_prefers_russian_then_falls_back() -> None:
    assert (
        promoted_description({"description:ru": "Русское", "description": "Neutral"}) == "Русское"
    )
    assert promoted_description({"description": "Neutral"}) == "Neutral"
    assert promoted_description({"description:en": "English"}) == "English"
    assert promoted_description({}) is None


def test_description_collapses_whitespace_and_caps_length() -> None:
    assert promoted_description({"description": "a\n\n  b\tc"}) == "a b c"
    long = promoted_description({"description": "x" * 5000})
    assert long is not None
    assert len(long) == 2000


def test_website_requires_http_scheme() -> None:
    assert promoted_website({"website": "https://example.org"}) == "https://example.org"
    assert promoted_website({"website": "http://example.org"}) == "http://example.org"
    # OSM carries bare hosts and junk in this tag; those must not become links.
    assert promoted_website({"website": "example.org"}) is None
    assert promoted_website({"website": "javascript:alert(1)"}) is None
    assert promoted_website({"contact:website": "https://fallback.example"}) == (
        "https://fallback.example"
    )


def test_phone_and_opening_hours_and_surface() -> None:
    assert promoted_phone({"contact:phone": "+7 978 000-00-00"}) == "+7 978 000-00-00"
    assert promoted_opening_hours({"opening_hours": "Mo-Fr 09:00-18:00"}) == "Mo-Fr 09:00-18:00"
    assert promoted_surface({"surface": "Gravel"}) == "gravel"
    assert promoted_surface({}) is None


def test_visit_minutes_takes_the_longest_category() -> None:
    # A spot that is both a mountain (120) and a viewpoint (30) is visited
    # for the mountain — under-estimating dwell time overfills a day plan.
    assert estimate_visit_minutes({"mountain", "viewpoint"}) == 120
    assert estimate_visit_minutes({"monument"}) == 20
    assert estimate_visit_minutes({"museum", "monument"}) == 90


def test_visit_minutes_falls_back_for_unknown_categories() -> None:
    assert estimate_visit_minutes(set()) == 45
    assert estimate_visit_minutes({"not-a-real-category"}) == 45


def test_safety_tags_from_payload_allowlists_and_caps_untrusted_osm() -> None:
    assert safety_tags_from_payload(None) is None
    assert safety_tags_from_payload({"tags": {}}) is None
    assert safety_tags_from_payload({"tags": {"name": "ignored", "tourism": "attraction"}}) is None
    tags = safety_tags_from_payload(
        {
            "tags": {
                "Access": "Private",
                "ford": "yes",
                "javascript": "alert(1)",
                "foot": "  YES  ",
                "waterway": "x" * 200,
            }
        }
    )
    assert tags is not None
    assert tags["access"] == "private"
    assert tags["ford"] == "yes"
    assert tags["foot"] == "yes"
    assert len(tags["waterway"]) == 64
    assert "javascript" not in tags
