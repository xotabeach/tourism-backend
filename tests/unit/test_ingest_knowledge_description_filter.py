"""`generated_draft` places are 82.6% a disclaimer template (measured
2026-09-03) — `_has_real_description` is what keeps it out of the knowledge
base so "требует редакционной проверки" never becomes retrieved context.
"""

import importlib.util
import pathlib

from tourism_backend.modules.places.infrastructure.models import Place

_SCRIPT_PATH = pathlib.Path(__file__).parents[2] / "scripts" / "ingest_knowledge.py"
_spec = importlib.util.spec_from_file_location("ingest_knowledge", _SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
ingest_knowledge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest_knowledge)


def _place(*, content_enrichment: dict[str, object] | None) -> Place:
    place = Place()
    place.content_enrichment = content_enrichment
    return place


def test_boilerplate_template_is_not_real_description() -> None:
    place = _place(content_enrichment={"prompt_version": "heuristic-v1"})
    assert ingest_knowledge._has_real_description(place) is False


def test_wikipedia_extract_is_real_description() -> None:
    place = _place(content_enrichment={"prompt_version": "heuristic-wikipedia-v1"})
    assert ingest_knowledge._has_real_description(place) is True


def test_missing_enrichment_metadata_defaults_to_real() -> None:
    # A place enriched by some future/other path this frozenset doesn't know
    # about should not be silently excluded — fail open, not closed.
    assert ingest_knowledge._has_real_description(_place(content_enrichment=None)) is True
    other = _place(content_enrichment={"prompt_version": "llm-v1"})
    assert ingest_knowledge._has_real_description(other) is True
