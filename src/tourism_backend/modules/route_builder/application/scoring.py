"""Deterministic catalog scoring for route match (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from tourism_backend.modules.route_builder.application.schemas import (
    DurationOption,
    PaceOption,
    RouteMatchParamsIn,
    TripType,
)

IDEAL_THRESHOLD = 0.55
CLOSE_THRESHOLD = 0.35
MAX_IDEAL = 3
MAX_CLOSE = 3

_DURATION_RANGES: dict[DurationOption, tuple[int, int]] = {
    "d1_2": (180, 1_440),
    "d3_5": (1_200, 4_320),
    "d6_7": (3_600, 6_480),
    "d7plus": (5_760, 20_160),
}

_PACE_DIFFICULTY: dict[PaceOption, set[str]] = {
    "calm": {"easy", "лёгкий", "легкий", "1", "2"},
    "moderate": {"moderate", "средний", "3"},
    "active": {"hard", "сложный", "4", "5", "difficult"},
}

_TRIP_KEYWORDS: dict[TripType, tuple[str, ...]] = {
    "romance": ("роман", "пар", "закат", "вид", "дворец", "море"),
    "rest": ("спокой", "пляж", "отдых", "релакс", "набереж"),
    "adventure": ("приключ", "экстрим", "тропа", "пещер", "скал"),
    "active": ("актив", "спорт", "велосипед", "треккинг", "поход"),
}

_INTEREST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "природа": ("природ", "лес", "парк", "водопад", "заповед"),
    "пляж": ("пляж", "море", "бухт", "набереж"),
    "горы": ("гор", "перевал", "вершин", "скал"),
    "еда": ("еда", "ресторан", "кафе", "вино", "дегустац"),
    "история": ("истори", "музей", "крепост", "дворец", "античн"),
    "экстрим": ("экстрим", "троллей", "дайв", "скалол"),
    "фото": ("фото", "видов", "смотров", "панорам"),
    "леса": ("лес", "рощ", "заповед"),
    "спорт": ("спорт", "велосипед", "бег", "йога"),
    "лошади": ("лошад", "конн", "верхов"),
}

# Public alias for place picker / generate pipeline.
INTEREST_KEYWORDS = _INTEREST_KEYWORDS


@dataclass(frozen=True, slots=True)
class RouteMatchCandidate:
    route_id: UUID
    name: str
    short_description: str | None
    description: str | None
    estimated_duration_minutes: int | None
    difficulty: str | None
    transport_mode: str | None
    seasonality: tuple[str, ...]
    suitable_for_children: bool | None
    pets_allowed: bool | None
    place_names: tuple[str, ...]
    locality_names: tuple[str, ...]
    stops_count: int


@dataclass(frozen=True, slots=True)
class ScoredMatch:
    candidate: RouteMatchCandidate
    score: float
    reasons: tuple[str, ...]


def _haystack(candidate: RouteMatchCandidate) -> str:
    parts = [
        candidate.name,
        candidate.short_description or "",
        candidate.description or "",
        " ".join(candidate.place_names),
        " ".join(candidate.locality_names),
        " ".join(candidate.seasonality),
    ]
    return " ".join(parts).casefold()


def _city_score(city: str, candidate: RouteMatchCandidate) -> tuple[float, str | None]:
    needle = city.casefold()
    localities = [name.casefold() for name in candidate.locality_names]
    places = [name.casefold() for name in candidate.place_names]
    name = candidate.name.casefold()
    if any(needle in loc or loc in needle for loc in localities if loc):
        return 1.0, f"старт рядом с {city}"
    if needle in name:
        return 0.85, f"в названии есть {city}"
    if any(needle in place for place in places):
        return 0.7, f"есть точки у {city}"
    # Soft partial: first 4+ chars
    if len(needle) >= 4 and (needle[:4] in name or any(needle[:4] in place for place in places)):
        return 0.35, None
    return 0.05, None


def _duration_score(
    duration: DurationOption,
    minutes: int | None,
) -> tuple[float, str | None]:
    if minutes is None or minutes <= 0:
        return 0.45, None
    low, high = _DURATION_RANGES[duration]
    if low <= minutes <= high:
        return 1.0, "длительность совпадает"
    if minutes < low:
        gap = (low - minutes) / max(low, 1)
        return max(0.15, 1.0 - gap), None
    gap = (minutes - high) / max(high, 1)
    return max(0.15, 1.0 - gap), None


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for kw in keywords if kw in text)


def _interests_score(interests: list[str], text: str) -> tuple[float, str | None]:
    if not interests:
        return 0.5, None
    hits = 0
    matched: list[str] = []
    for interest in interests:
        key = interest.casefold()
        stems = _INTEREST_KEYWORDS.get(key, (key,))
        if _keyword_hits(text, stems) > 0 or key in text:
            hits += 1
            matched.append(interest)
    ratio = hits / len(interests)
    reason = f"интересы: {', '.join(matched)}" if matched else None
    return ratio, reason


def _trip_type_score(trip_type: TripType | None, text: str) -> tuple[float, str | None]:
    if trip_type is None:
        return 0.5, None
    keywords = _TRIP_KEYWORDS[trip_type]
    hits = _keyword_hits(text, keywords)
    if hits >= 2:
        return 1.0, f"тип «{trip_type}»"
    if hits == 1:
        return 0.7, f"тип «{trip_type}»"
    return 0.25, None


def _pace_score(pace: PaceOption, difficulty: str | None) -> tuple[float, str | None]:
    if not difficulty:
        return 0.5, None
    allowed = _PACE_DIFFICULTY[pace]
    if difficulty.casefold() in allowed:
        return 1.0, "темп подходит"
    return 0.35, None


_TRANSPORT_CANONICAL: dict[str, str] = {
    "walk": "walk",
    "walking": "walk",
    "car": "car",
    "public": "public",
    "public_transport": "public",
    "mixed": "mixed",
}


def _normalize_transport(value: str | None) -> str | None:
    if not value:
        return None
    return _TRANSPORT_CANONICAL.get(value.casefold().strip(), value.casefold().strip())


def _transport_score(
    requested: str | None,
    actual: str | None,
) -> tuple[float, str | None]:
    if not requested:
        return 0.5, None
    req = _normalize_transport(requested)
    act = _normalize_transport(actual)
    if not act:
        return 0.4, None
    if req == act or req == "mixed":
        return 1.0, f"транспорт: {act}"
    return 0.2, None


_SEASON_ALIASES: dict[str, tuple[str, ...]] = {
    "весна": ("весна", "spring"),
    "лето": ("лето", "summer"),
    "осень": ("осень", "autumn", "fall"),
    "зима": ("зима", "winter"),
}


def _season_score(season: str | None, seasonality: tuple[str, ...]) -> tuple[float, str | None]:
    if not season:
        return 0.5, None
    if not seasonality:
        return 0.4, None
    needle = season.casefold()
    aliases = _SEASON_ALIASES.get(needle, (needle,))
    catalog = tuple(item.casefold() for item in seasonality)
    if any(
        alias in item or item in alias or alias == item for alias in aliases for item in catalog
    ):
        return 1.0, f"сезон: {season}"
    return 0.2, None


def _party_flags_score(
    params: RouteMatchParamsIn,
    candidate: RouteMatchCandidate,
) -> tuple[float, str | None]:
    score = 0.5
    reasons: list[str] = []
    if params.with_children is True:
        if candidate.suitable_for_children is True:
            score = 1.0
            reasons.append("можно с детьми")
        elif candidate.suitable_for_children is False:
            score = 0.1
    if params.with_pets is True:
        if candidate.pets_allowed is True:
            score = max(score, 0.9)
            reasons.append("можно с питомцами")
        elif candidate.pets_allowed is False:
            score = min(score, 0.15)
    if params.people >= 6 and candidate.stops_count >= 4:
        score = min(1.0, score + 0.1)
    reason = ", ".join(reasons) if reasons else None
    return score, reason


def score_candidate(params: RouteMatchParamsIn, candidate: RouteMatchCandidate) -> ScoredMatch:
    text = _haystack(candidate)
    parts: list[tuple[float, float, str | None]] = []
    # (weight, score, reason)
    c_score, c_reason = _city_score(params.city, candidate)
    parts.append((0.32, c_score, c_reason))
    d_score, d_reason = _duration_score(params.duration, candidate.estimated_duration_minutes)
    parts.append((0.18, d_score, d_reason))
    i_score, i_reason = _interests_score(params.interests, text)
    parts.append((0.2, i_score, i_reason))
    t_score, t_reason = _trip_type_score(params.trip_type, text)
    parts.append((0.12, t_score, t_reason))
    p_score, p_reason = _pace_score(params.pace, candidate.difficulty)
    parts.append((0.08, p_score, p_reason))
    tr_score, tr_reason = _transport_score(params.transport_mode, candidate.transport_mode)
    parts.append((0.05, tr_score, tr_reason))
    s_score, s_reason = _season_score(params.season, candidate.seasonality)
    parts.append((0.03, s_score, s_reason))
    f_score, f_reason = _party_flags_score(params, candidate)
    parts.append((0.02, f_score, f_reason))

    total_w = sum(w for w, _, _ in parts)
    score = sum(w * s for w, s, _ in parts) / total_w
    reasons = [r for _, _, r in parts if r]
    # Soft penalty if city almost unmatched
    if c_score < 0.3:
        score *= 0.55
    return ScoredMatch(
        candidate=candidate,
        score=round(min(1.0, max(0.0, score)), 4),
        reasons=tuple(reasons[:6]),
    )


def partition_scored(
    scored: list[ScoredMatch],
) -> tuple[list[ScoredMatch], list[ScoredMatch], bool]:
    ordered = sorted(scored, key=lambda item: (-item.score, item.candidate.name))
    ideal = [item for item in ordered if item.score >= IDEAL_THRESHOLD][:MAX_IDEAL]
    close = [
        item
        for item in ordered
        if CLOSE_THRESHOLD <= item.score < IDEAL_THRESHOLD and item not in ideal
    ][:MAX_CLOSE]
    offer_generate = len(ideal) == 0
    return ideal, close, offer_generate
