"""What goes into the knowledge index for a place and a route (spec 18).

Shared by the nightly reconciliation job and `scripts/ingest_knowledge.py`,
so both index the same text.
"""

from __future__ import annotations

from typing import Any

_BOILERPLATE_PROMPT_VERSIONS = frozenset({"heuristic-v1"})
# The literal marker `content_enrichment.py` writes into `description` when
# no source text existed. Checked directly rather than trusted to imply
# `prompt_version` == "heuristic-v1": three seed places (Ливадийский дворец,
# Херсонес Таврический, Долина привидений) carried this exact text in
# `description` with `content_enrichment_status = "missing"` and no
# `prompt_version` at all. Metadata drifted; the string in the column that
# actually gets indexed did not.
_BOILERPLATE_MARKER = "Описание сгенерировано автоматически как черновик"

_LEVELS = {
    1: "лёгкий",
    2: "несложный",
    3: "средней сложности",
    4: "трудный",
    5: "очень трудный",
}


def has_real_description(place: Any) -> bool:
    """True unless `description` is the templated placeholder.

    `content_enrichment.prompt_version` distinguishes the two heuristics in
    `content_enrichment.py`: "heuristic-wikipedia-v1" wraps a real extract,
    "heuristic-v1" is the "Описание сгенерировано автоматически..." template
    used when no source text existed. Indexing it teaches the retriever
    nothing about the place and puts "требует редакционной проверки" one
    retrieval away from a user's screen.
    """
    description = place.description or ""
    if _BOILERPLATE_MARKER in description:
        return False
    enrichment = place.content_enrichment
    if not isinstance(enrichment, dict):
        return True
    return enrichment.get("prompt_version") not in _BOILERPLATE_PROMPT_VERSIONS


def route_facts(
    route: Any,
    *,
    stops: list[tuple[int, str]],
    days: list[tuple[int, int, int, str | None]],
) -> str:
    """What the agent matches a request against besides the prose: how the
    route is travelled, how long and hard it is, its tags and its stops by
    day. Numbers come from the stored calculation, never from the text.

    ``stops`` are (position, name); ``days`` are (index, first position,
    last position, overnight note)."""
    lines = ["## Параметры"]
    lines.append("Способ: " + ("на машине" if route.base_mode != "walk" else "пешком"))
    if len(days) > 1:
        lines.append(f"Дней: {len(days)} (многодневный маршрут)")
    elif route.estimated_duration_minutes:
        hours = route.estimated_duration_minutes / 60
        lines.append("Длительность: " + ("полдня" if hours <= 4.5 else "один день"))
    if route.distance_meters:
        lines.append(f"Длина пути: {route.distance_meters / 1000:.1f} км")
    if route.difficulty_level:
        lines.append(
            f"Сложность: {route.difficulty_level} из 5, {_LEVELS.get(route.difficulty_level, '')}"
        )
    filters = (route.accessibility or {}).get("filters") or []
    tags = [str(tag) for tag in filters if isinstance(tag, str)]
    if route.is_seaside and "Море" not in tags:
        tags.append("Море")
    if tags:
        lines.append("Метки: " + ", ".join(tags))
    if route.suitable_for_children:
        lines.append("Подходит для поездки с детьми")
    if route.source == "editorial":
        lines.append("Автор: редакция КРЫМТРИП")
    lines.append("\n## Точки маршрута")
    if len(days) > 1:
        for index, first, last, night in days:
            names = [name for position, name in stops if first <= position <= last]
            line = f"День {index}: " + ", ".join(names)
            if night:
                line += f". {night}"
            lines.append(line)
    else:
        lines.append(", ".join(name for _position, name in stops))
    return "\n".join(lines)


def route_description(description: str | None, facts: str) -> str:
    """The route's prose followed by its facts, as one indexed document."""
    return "\n\n".join(part for part in (description, facts) if part)
