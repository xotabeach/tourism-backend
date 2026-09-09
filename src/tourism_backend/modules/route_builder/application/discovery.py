"""Destination discovery is not a commitment to a departure city or itinerary."""

import re
from typing import Any

SOUTH_COAST = "Южный берег Крыма"
SOUTH_COAST_LOCALITIES = (
    "Форос",
    "Симеиз",
    "Алупка",
    "Ялта",
    "Гаспра",
    "Кореиз",
    "Ливадия",
    "Массандра",
    "Никита",
    "Гурзуф",
    "Партенит",
    "Алушта",
    "Кацивели",
    "Понизовка",
)
_AREAS = (
    (r"\b(?:юбк|южн\w*\s+берег\w*)\b", SOUTH_COAST),
    (r"\b(?:крым\w*|crimea)\b", "Крым"),
)
_TOWNS = {
    "форос": "Форос",
    "симеиз": "Симеиз",
    "ялт": "Ялта",
    "алупк": "Алупка",
    "алушт": "Алушта",
    "гурзуф": "Гурзуф",
    "судак": "Судак",
    "евпатори": "Евпатория",
    "бахчисара": "Бахчисарай",
    "севастопол": "Севастополь",
    "феодоси": "Феодосия",
    "керч": "Керчь",
    "симферопол": "Симферополь",
    "новый свет": "Новый Свет",
}


def discovery_patch(text: str) -> dict[str, Any]:
    """Conservative outage-safe hints, not the main model's full NLU.

    Towns mentioned as examples are soft interests, never mandatory stops.
    Unknown geography is left to the provider/catalogue, not guessed.
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
    towns = [town for stem, town in _TOWNS.items() if re.search(r"\b" + stem + r"\w*\b", folded)]
    if towns:
        patch["preferred_localities"] = towns[:8]
        patch.setdefault("search_area", towns[0] if len(towns) == 1 else "Крым")
    if re.search(r"(?:любой\s+(?:город|старт)|откуда\s*(?:то|угодно))", folded):
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
    if area == SOUTH_COAST:
        return SOUTH_COAST_LOCALITIES
    if area.casefold() in {"крым", "crimea", "весь крым"}:
        return ()
    return (area,)
