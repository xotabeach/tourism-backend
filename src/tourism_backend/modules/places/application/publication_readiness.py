"""Mechanical publication gate for places (ADR-009 follow-up).

5000 OSM places sit at `publication_status='draft'` while `place_picker`
selects `published` only, so route generation still sees the 20 seed places.
Publishing them wholesale is not an option: OSM carries survey markers whose
"description" is just their own code, and machine-drafted text is filler.

This module answers one question — *may this place be published at all?* —
and deliberately answers it mechanically. It is a floor, not editorial
approval: an admin still has to act, and that action is audited.

Blockers vs warnings:

- **Blockers** make the catalog card wrong or unusable (no city to filter
  by, no category to match on, nothing to read, place is closed).
- **Warnings** degrade gracefully and must not stop publication — a missing
  photo already falls back through `place_covers.generic_fallback_cover`.

Machine-drafted text is explicitly *not* enough. `heuristic_content_draft`
produces "<name> — <categories> в <city>.", which restates the title and
would turn this gate into theatre if it counted as content.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Text written by `content_enrichment`, pending a human read.
GENERATED_DRAFT = "generated_draft"
EDITORIAL_REVIEWED = "editorial_reviewed"

#: Below this a "description" is a label, not content (OSM survey markers
#: routinely carry their own identifier as the description).
_MIN_MEANINGFUL_TEXT_CHARS = 24

PUBLISHABLE_STATUSES = frozenset({"draft", "rejected"})


@dataclass(frozen=True, slots=True)
class PlacePublicationFacts:
    """Everything the gate needs, decoupled from the ORM for testability."""

    name: str | None
    has_locality: bool
    category_count: int
    short_description: str | None
    description: str | None
    content_enrichment_status: str
    has_cover_photo: bool
    temporary_closure_status: str | None


def _is_usable_name(raw: str | None) -> bool:
    """A catalog card needs a title, not an annotation.

    OSM labels unnamed features with parenthetical notes — a spring tagged
    "(плохая вода)" is a hiker's remark about the water, not a place name,
    and it reads as nonsense as a card heading.
    """
    if not raw:
        return False
    name = raw.strip()
    if len(name) < 2:
        return False
    if not any(char.isalpha() for char in name):
        return False
    return not (name.startswith("(") and name.endswith(")"))


def meaningful_text_ignoring_status(
    *, name: str | None, short_description: str | None, description: str | None
) -> str | None:
    """Longest candidate clearing the content bar, without the draft check.

    Split out of `_meaningful_text` so `enrich_places_content.py` can ask
    "does this place already have real content?" *before* writing a machine
    draft into an empty field alongside it — the answer must not depend on
    `content_enrichment_status`, which is exactly the field about to change.
    """
    candidates = [
        text.strip() for text in (description, short_description) if text and text.strip()
    ]
    if not candidates:
        return None
    best = max(candidates, key=len)
    if len(best) < _MIN_MEANINGFUL_TEXT_CHARS:
        return None
    # A description that merely restates the name adds nothing.
    if name and best.casefold().strip(" .·—-") == name.casefold().strip():
        return None
    return best


def _meaningful_text(facts: PlacePublicationFacts) -> str | None:
    """Longest human-written text, or None when only filler is present."""
    if facts.content_enrichment_status == GENERATED_DRAFT:
        # Machine draft not yet read by a human — does not count as content.
        return None
    return meaningful_text_ignoring_status(
        name=facts.name,
        short_description=facts.short_description,
        description=facts.description,
    )


def publication_blockers(facts: PlacePublicationFacts) -> tuple[str, ...]:
    """Reasons this place must not be published yet, in Russian for the admin."""
    blockers: list[str] = []
    if not _is_usable_name(facts.name):
        blockers.append("нет пригодного названия")
    if not facts.has_locality:
        blockers.append("не привязано к городу")
    if facts.category_count < 1:
        blockers.append("нет категории")
    if _meaningful_text(facts) is None:
        if facts.content_enrichment_status == GENERATED_DRAFT:
            blockers.append("текст сгенерирован, нужна проверка редактора")
        else:
            blockers.append("нет содержательного описания")
    if facts.temporary_closure_status in {"closed", "permanently_closed"}:
        blockers.append("место закрыто")
    return tuple(blockers)


def publication_warnings(facts: PlacePublicationFacts) -> tuple[str, ...]:
    """Quality gaps worth showing an editor that do not block publication."""
    warnings: list[str] = []
    if not facts.has_cover_photo:
        warnings.append("нет своей фотографии (подставится общая)")
    if facts.content_enrichment_status == EDITORIAL_REVIEWED and not facts.description:
        warnings.append("проверено редактором, но нет полного описания")
    if facts.temporary_closure_status == "partial":
        warnings.append("частичные ограничения доступа")
    return tuple(warnings)


def is_ready_for_publication(facts: PlacePublicationFacts) -> bool:
    return not publication_blockers(facts)
