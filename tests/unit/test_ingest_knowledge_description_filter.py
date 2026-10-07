"""`generated_draft` places are 82.6% a disclaimer template (measured
2026-09-03) — `_has_real_description` is what keeps it out of the knowledge
base so "требует редакционной проверки" never becomes retrieved context.
"""

import importlib.util
import pathlib
from types import SimpleNamespace

from tourism_backend.modules.places.infrastructure.models import Place

_SCRIPT_PATH = pathlib.Path(__file__).parents[2] / "scripts" / "ingest_knowledge.py"
_spec = importlib.util.spec_from_file_location("ingest_knowledge", _SCRIPT_PATH)
assert _spec is not None
assert _spec.loader is not None
ingest_knowledge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ingest_knowledge)


def _place(*, content_enrichment: dict[str, object] | None, description: str = "") -> Place:
    place = Place()
    place.content_enrichment = content_enrichment
    place.description = description
    return place


def test_boilerplate_template_is_not_real_description() -> None:
    place = _place(
        content_enrichment={"prompt_version": "heuristic-v1"},
        description=(
            "Тестовое место — туристическое место (Горы) в Ялта. "
            "Описание сгенерировано автоматически как черновик и требует "
            "редакционной проверки."
        ),
    )
    assert ingest_knowledge._has_real_description(place) is False


def test_boilerplate_text_is_rejected_even_with_stale_or_missing_metadata() -> None:
    # Ливадийский дворец / Херсонес Таврический / Долина привидений
    # (2026-09-03): content_enrichment_status was reset to "missing" and
    # prompt_version is absent, but the description column itself still
    # carries the template. The text is the ground truth, not the metadata.
    place = _place(
        content_enrichment=None,
        description="Х — туристическое место (Музеи) в Севастополь. "
        "Описание сгенерировано автоматически как черновик и требует "
        "редакционной проверки.",
    )
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


def _route(**fields: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "base_mode": "car",
        "estimated_duration_minutes": 906,
        "distance_meters": 249_583,
        "difficulty_level": 1,
        "accessibility": {"filters": ["На машине", "История"]},
        "is_seaside": True,
        "suitable_for_children": True,
        "source": "editorial",
    }
    return SimpleNamespace(**{**base, **fields})


def test_route_facts_carry_what_a_request_is_matched_against() -> None:
    """BACKEND-18: the agent needs the mode, days, length, level, tags and the
    stops by day, not only the prose."""
    facts = ingest_knowledge.route_facts(
        _route(),
        stops=[(1, "Херсонес Таврический"), (2, "Ласточкино гнездо"), (3, "Ханский дворец")],
        days=[(1, 1, 1, "Ночлег: Балаклава"), (2, 2, 2, "Ночлег: Ялта"), (3, 3, 3, None)],
    )
    assert "Способ: на машине" in facts
    assert "Дней: 3 (многодневный маршрут)" in facts
    assert "Длина пути: 249.6 км" in facts
    assert "Сложность: 1 из 5, лёгкий" in facts
    assert "Метки: На машине, История, Море" in facts
    assert "Подходит для поездки с детьми" in facts
    assert "Автор: редакция КРЫМТРИП" in facts
    assert "День 1: Херсонес Таврический. Ночлег: Балаклава" in facts
    assert "День 3: Ханский дворец" in facts


def test_route_facts_of_a_short_walk_have_no_days() -> None:
    facts = ingest_knowledge.route_facts(
        _route(
            base_mode="walk",
            estimated_duration_minutes=190,
            distance_meters=1_700,
            accessibility=None,
            is_seaside=False,
            suitable_for_children=False,
            source="user_created",
        ),
        stops=[(1, "Набережная"), (2, "Церковь Илии Пророка")],
        days=[(1, 1, 2, None)],
    )
    assert "Способ: пешком" in facts
    assert "Длительность: полдня" in facts
    assert "Дней:" not in facts
    assert "Метки" not in facts
    assert "КРЫМТРИП" not in facts
    assert facts.endswith("Набережная, Церковь Илии Пророка")
