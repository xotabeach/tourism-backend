"""Destination discovery is not a commitment to a departure city or itinerary."""

import re
from typing import Any

SOUTH_COAST = "Южный берег Крыма"
# Product areas are geometry, not lists of “important cities”.  Bounds are a
# coarse catalogue pre-filter; exact route geometry remains authoritative.
_AREA_BOUNDS: dict[str, tuple[float, float, float, float]] = {
    # west, south, east, north
    SOUTH_COAST: (33.70, 44.35, 34.60, 44.72),
}
_AREAS = (
    (r"\b(?:юбк|южн\w*\s+берег\w*)\b", SOUTH_COAST),
    (r"\b(?:крым\w*|crimea)\b", "Крым"),
)


def discovery_patch(text: str) -> dict[str, Any]:
    """Conservative outage-safe hints, not the main model's full NLU.

    Named localities are resolved from the database by ``session_service``;
    this outage fallback deliberately has no embedded town whitelist.
    """
    folded = text.casefold().replace("ё", "е")
    patch: dict[str, Any] = {}
    # Do not turn a historical question or a rejected destination into a filter.
    # Negation needs the main model's contextual extraction, not stem matching.
    if re.search(r"\b(?:не|кроме|исключи|без)\b", folded):
        if re.search(
            r"(?:не\s+нужен.{0,12}(?:город|старт)|(?:город|старт).{0,15}не\s+нужен|"
            r"неваж\w*.{0,15}(?:старт|откуда)|откуда\s*(?:то|угодно))",
            folded,
        ):
            patch["flexible_start"] = True
        return patch
    if not wants_discovery(text) and len(text.strip()) > 40:
        return patch
    for pattern, area in _AREAS:
        if re.search(pattern, folded):
            patch["search_area"] = area
            break
    if re.search(r"\b(?:пеш(?:ком|ий|ая|ую)|прогулк\w*|гуля(?:ть|ем))\b", folded):
        patch["transport_mode"] = "walk"
    elif re.search(r"\b(?:на\s+машин\w*|авто(?:мобил\w*)?)\b", folded):
        patch["transport_mode"] = "car"
    elif re.search(r"\bобщественн\w*\s+транспорт\w*\b", folded):
        patch["transport_mode"] = "public"
    interests: list[str] = []
    for pattern, interest in (
        (r"\b(?:мор\w*|пляж\w*|побереж\w*)\b", "море"),
        (r"\b(?:гор\w*|скал\w*|вершин\w*)\b", "горы"),
        (r"\b(?:истори\w*|дворц\w*|крепост\w*|музе\w*)\b", "история"),
        (r"\b(?:природ\w*|парк\w*|лес\w*)\b", "природа"),
    ):
        if re.search(pattern, folded):
            interests.append(interest)
    if interests:
        patch["interests_add"] = interests
    if re.search(
        r"(?:любой\s+(?:город|старт)|откуда\s*(?:то|угодно)|"
        r"выбер\w*\s+сам|предлаг\w*\s+сам|реши\s+сам)",
        folded,
    ):
        patch["flexible_start"] = True
    return patch


def wants_discovery(text: str) -> bool:
    return bool(
        re.search(
            r"подобра|подбер|посовет|предлаг|рекоменд|маршрут|вариант|что\s+посмотр|куда\s+по",
            text.casefold(),
        )
    )


def area_localities(area: str) -> tuple[str, ...]:
    if area.casefold() in {"крым", "crimea", "весь крым"}:
        return ()
    return (area,)


def area_bounds(area: str) -> tuple[float, float, float, float] | None:
    return _AREA_BOUNDS.get(area)
