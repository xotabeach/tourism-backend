"""Unit tests for the mechanical place publication gate."""

from __future__ import annotations

from tourism_backend.modules.places.application.publication_readiness import (
    PlacePublicationFacts,
    is_ready_for_publication,
    publication_blockers,
    publication_warnings,
)

_GOOD_TEXT = "Средневековая крепость на скале над морем, построена генуэзцами."


def _facts(**overrides: object) -> PlacePublicationFacts:
    base: dict[str, object] = {
        "name": "Генуэзская крепость",
        "has_locality": True,
        "category_count": 2,
        "short_description": None,
        "description": _GOOD_TEXT,
        "content_enrichment_status": "missing",
        "has_cover_photo": True,
        "temporary_closure_status": None,
    }
    base.update(overrides)
    return PlacePublicationFacts(**base)  # type: ignore[arg-type]


def test_complete_place_is_ready() -> None:
    assert is_ready_for_publication(_facts()) is True
    assert publication_blockers(_facts()) == ()


def test_annotation_instead_of_name_blocks() -> None:
    """OSM labels unnamed features with parenthetical remarks.

    A spring tagged "(плохая вода)" is a hiker's note about the water, not
    a place name, and reads as nonsense as a catalog card heading — even
    though its description is genuine human-written text.
    """
    facts = _facts(name="(плохая вода)", description="Стоячая лужа, я бы не пил (май 2025)")
    assert is_ready_for_publication(facts) is False
    assert "нет пригодного названия" in publication_blockers(facts)


def test_nameless_and_punctuation_only_names_block() -> None:
    for bad in (None, "", "   ", "-", "?!", "()"):
        assert is_ready_for_publication(_facts(name=bad)) is False


def test_missing_locality_blocks() -> None:
    blockers = publication_blockers(_facts(has_locality=False))
    assert "не привязано к городу" in blockers
    assert is_ready_for_publication(_facts(has_locality=False)) is False


def test_missing_category_blocks() -> None:
    assert "нет категории" in publication_blockers(_facts(category_count=0))


def test_missing_text_blocks() -> None:
    blockers = publication_blockers(_facts(description=None, short_description=None))
    assert "нет содержательного описания" in blockers


def test_machine_drafted_text_does_not_count_as_content() -> None:
    """heuristic_content_draft writes "<name> — <categories> в <city>."

    Accepting that as content would make the gate theatre, so a place whose
    text is still `generated_draft` is blocked until an editor reads it.
    """
    facts = _facts(
        description="Генуэзская крепость — Крепости, Музеи в Судаке.",
        content_enrichment_status="generated_draft",
    )
    assert is_ready_for_publication(facts) is False
    assert "текст сгенерирован, нужна проверка редактора" in publication_blockers(facts)


def test_editorially_reviewed_text_counts() -> None:
    facts = _facts(content_enrichment_status="editorial_reviewed")
    assert is_ready_for_publication(facts) is True


def test_description_that_only_restates_the_name_blocks() -> None:
    # Real OSM survey markers carry their own code as the description.
    facts = _facts(name="SMX-29023", description="SMX-29023")
    assert is_ready_for_publication(facts) is False


def test_too_short_description_blocks() -> None:
    assert is_ready_for_publication(_facts(description="Пляж")) is False


def test_closed_place_blocks_but_partial_only_warns() -> None:
    assert is_ready_for_publication(_facts(temporary_closure_status="closed")) is False
    partial = _facts(temporary_closure_status="partial")
    assert is_ready_for_publication(partial) is True
    assert "частичные ограничения доступа" in publication_warnings(partial)


def test_missing_photo_warns_but_never_blocks() -> None:
    """generic_fallback_cover already degrades gracefully for missing photos."""
    facts = _facts(has_cover_photo=False)
    assert is_ready_for_publication(facts) is True
    assert "нет своей фотографии (подставится общая)" in publication_warnings(facts)


def test_short_description_alone_is_enough() -> None:
    facts = _facts(description=None, short_description=_GOOD_TEXT)
    assert is_ready_for_publication(facts) is True
