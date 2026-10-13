"""Deterministic catalog scoring for route match (no LLM)."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from tourism_backend.modules.route_builder.application.schemas import (
    DurationOption,
    PaceOption,
    RouteMatchParamsIn,
    TripType,
)
from tourism_backend.modules.routes.application.difficulty import (
    ProfileFit,
    level_from_legacy,
    profile_fit,
)

# Bump when the meaning of the score changes; stored with chat snapshots.
MATCH_FORMULA_VERSION = 2

IDEAL_THRESHOLD = 0.55
# Lowest exact score that is shown at all (spec 06, D3).
MIN_MATCH_SCORE = 0.30
MAX_HITS = 8
# Old app versions only know two arrays of at most 3 routes each and start
# them at 35%; they keep receiving exactly that shape (D15).
CLOSE_THRESHOLD = 0.35
MAX_IDEAL = 3
MAX_CLOSE = 3

# Ceilings that keep the percent honest (D12, D15, D16).
PARTIAL_DATA_CAP = 0.5
PENALTY_CAP = 0.45
# Known signals must cover this share of the requested weight, else "partial".
MIN_KNOWN_SHARE = 0.5
# The chat shows a percent only once this many parameters are confirmed (D7).
MIN_CHAT_SIGNALS = 3

#: Weights of transport and season in a candidate's score (BACKEND-42).
#: They were 0.05 and 0.03: a car route still came first for «Пешком», and a
#: winter route was barely marked down in summer.
TRANSPORT_WEIGHT = 0.15
SEASON_WEIGHT = 0.10
SEASON_WEIGHT_BY_DATE = 0.06

_MONTH_SEASON = {
    12: "зима",
    1: "зима",
    2: "зима",
    3: "весна",
    4: "весна",
    5: "весна",
    6: "лето",
    7: "лето",
    8: "лето",
    9: "осень",
    10: "осень",
    11: "осень",
}


def season_for(day: date) -> str:
    """The season a date falls in, as the catalogue names it."""
    return _MONTH_SEASON[day.month]


def today() -> date:
    """Today in Crimea, where the trips happen."""
    return datetime.now(ZoneInfo("Europe/Simferopol")).date()


_DURATION_RANGES: dict[DurationOption, tuple[int, int]] = {
    "h2": (30, 180),
    "half_day": (120, 360),
    "d1_2": (180, 1_440),
    "d3_5": (1_200, 4_320),
    "d6_7": (3_600, 6_480),
    "d7plus": (5_760, 20_160),
}

_PACE_DIFFICULTY: dict[PaceOption, set[str]] = {
    "calm": {"easy", "лёгкий", "легкий", "1", "2"},
    "moderate": {"moderate", "средний", "3"},
    "active": {"hard", "extreme", "очень сложный", "сложный", "4", "5", "difficult"},
}

_TRIP_KEYWORDS: dict[TripType, tuple[str, ...]] = {
    "romance": ("роман", "закат", "вид", "дворец", "море"),
    "rest": ("спокой", "пляж", "отдых", "релакс", "набереж"),
    "adventure": ("приключ", "экстрим", "тропа", "пещер", "скал"),
    "active": ("актив", "спорт", "велосипед", "треккинг", "поход"),
    "photo": ("вид", "смотров", "панорам", "закат", "фото", "скал", "обрыв"),
}

_INTEREST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "природа": ("природ", "лес", "парк", "водопад", "заповед"),
    "пляж": ("пляж", "море", "бухт", "набереж"),
    # «гор» alone also matched «город» (BACKEND-68): see _STEM_EXCLUSIONS.
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

# Category taxonomy is the only signal with full coverage (100% of places carry
# at least one `place_categories` row), whereas the free text these keywords
# search is filled for well under 1% of imported places. Categories are
# therefore the primary interest signal and the keywords a fallback — see
# ADR-009 (data-first route intelligence).
INTEREST_CATEGORIES: dict[str, frozenset[str]] = {
    "природа": frozenset({"nature", "park", "waterfall", "trail"}),
    "пляж": frozenset({"beach"}),
    # A viewpoint is in a third of all routes, in a city as often as at
    # sea or in the mountains: it made «море» and «горы» match nearly
    # everything (BACKEND-68). The sea comes from beaches and the route's
    # «Море» tag, the mountains from mountains, caves and trails.
    "море": frozenset({"beach"}),
    "горы": frozenset({"mountain", "cave", "trail"}),
    "еда": frozenset({"winery"}),
    "лес": frozenset({"nature", "park", "trail"}),
    "леса": frozenset({"nature", "park", "trail"}),
    "вино": frozenset({"winery"}),
    "история": frozenset(
        {"museum", "fortress", "palace", "monument", "religious-site", "landmark"}
    ),
    "экстрим": frozenset({"cave", "mountain", "trail"}),
    "фото": frozenset({"viewpoint", "waterfall", "palace"}),
    "спорт": frozenset({"trail", "mountain"}),
    "лошади": frozenset({"trail", "nature"}),
    "романтика": frozenset({"palace", "viewpoint", "beach", "park"}),
    # Profile quiz vocabulary (FRONTEND-21).
    "гастрономия": frozenset({"winery"}),
    "смотровые": frozenset({"viewpoint"}),
    "семейное": frozenset({"park", "beach", "museum", "nature"}),
}

TRIP_TYPE_CATEGORIES: dict[TripType, frozenset[str]] = {
    "romance": frozenset({"palace", "viewpoint", "beach", "park"}),
    "rest": frozenset({"beach", "park", "nature"}),
    "adventure": frozenset({"cave", "mountain", "trail", "waterfall"}),
    "active": frozenset({"trail", "mountain", "cave"}),
    # Кадр делают виды, вода и архитектура — пещеры и тропы сами по себе нет.
    "photo": frozenset({"viewpoint", "waterfall", "beach", "mountain", "palace"}),
}


# Interests the route's «Море» tag answers on its own (BACKEND-19).
SEASIDE_INTERESTS = frozenset({"море", "пляж"})


def categories_for_interest(interest: str) -> frozenset[str]:
    return INTEREST_CATEGORIES.get(interest.casefold().strip(), frozenset())


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
    # Locality of every stop in order, None where a place has none.
    stop_localities: tuple[str | None, ...] = ()
    stop_coordinates: tuple[tuple[float, float], ...] = ()
    # Distinct category slugs across the route's stops (ADR-009: primary
    # interest/trip-type signal, since it is the one field with full coverage).
    category_slugs: frozenset[str] = frozenset()
    typical_crowding: str = "unknown"
    price_min_amount: int | None = None
    # Editor's «Море» tag (BACKEND-19).
    is_seaside: bool = False
    # Spec 17: shown level 1..5 and the walking part's level.
    difficulty_level: int | None = None
    walk_level: int | None = None


@dataclass(frozen=True, slots=True)
class UserPreferenceSignals:
    """Explicit profile preferences used as a bounded ranking signal.

    Preferences are intentionally soft: an explicit request in the current
    session always remains the dominant signal, and an unknown route field is
    neutral rather than a penalty.  Behavioural history is not represented
    here; it belongs to the later feedback/ranker workstream.
    """

    categories: frozenset[str] = frozenset()
    difficulty: str | None = None
    # «На транспорте» (``car``): matched by the walking part (spec 17).
    transport: str | None = None
    travels_with_kids: bool = False
    travels_with_pets: bool = False


@dataclass(frozen=True, slots=True)
class ScoredMatch:
    candidate: RouteMatchCandidate
    score: float
    reasons: tuple[str, ...]
    # Most important first; the first one is what a card shows.
    mismatches: tuple[str, ...] = ()
    # Known signals covered too little of what was asked, or safety data is missing.
    partial_data: bool = False
    # Transport/paid penalty or unknown safety data: not for old clients' arrays.
    has_violation: bool = False
    # Every requested signal was known and matched completely.
    full_match: bool = False
    requested_signals: int = 0
    excluded: bool = False
    #: How strongly it answers among equal scores (BACKEND-68): the share of
    #: stops in the named towns and how many of its kinds of places answer
    #: the interests. Ranks a route with five beaches above one with a pier.
    depth: float = 0.0


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


def _location_score(
    location: str | None, candidate: RouteMatchCandidate
) -> tuple[float, str | None]:
    if not location:
        return 0.75, "старт можно выбрать автоматически"
    needle = location.casefold()
    if needle == "крым":
        return 0.75, "маршрут по Крыму"
    localities = [name.casefold() for name in candidate.locality_names]
    places = [name.casefold() for name in candidate.place_names]
    name = candidate.name.casefold()
    if any(needle in loc or loc in needle for loc in localities if loc):
        return 1.0, f"есть точки в районе: {location}"
    if needle in name:
        return 0.85, f"в названии есть {location}"
    if any(needle in place for place in places):
        return 0.7, f"есть точки у {location}"
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
        # Trip length is available time, not a requirement to spend every
        # minute on one route. Day trips belong in a multi-day shortlist.
        return 0.85, "можно пройти за часть поездки"
    gap = (minutes - high) / max(high, 1)
    return max(0.15, 1.0 - gap), None


# Stems that start other words: «гор» is also «город», «горожане».
_STEM_EXCLUSIONS: dict[str, str] = {"гор": "од|ожан", "вид": "ео"}


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    """How many stems start a word in [text] (BACKEND-68).

    A plain substring test made «гор» hit «город» and «парк» answer «пар».
    """
    hits = 0
    for kw in keywords:
        exclusion = _STEM_EXCLUSIONS.get(kw)
        pattern = rf"(?<![а-яёa-z]){re.escape(kw)}" + (f"(?!{exclusion})" if exclusion else "")
        if re.search(pattern, text):
            hits += 1
    return hits


def _interests_score(
    interests: list[str],
    text: str,
    categories: frozenset[str] = frozenset(),
    *,
    seaside: bool = False,
) -> tuple[float, str | None]:
    """Tag and category match first, free-text keywords as fallback.

    Text alone yields false negatives on imported places (description is
    filled for ~0.1% of them), so an interest counts as hit when either the
    taxonomy or the text agrees.
    """
    if not interests:
        return 0.5, None
    hits = 0
    matched: list[str] = []
    for interest in interests:
        key = interest.casefold()
        by_tag = seaside and key in SEASIDE_INTERESTS
        by_category = by_tag or bool(categories & categories_for_interest(key))
        stems = _INTEREST_KEYWORDS.get(key, (key,))
        by_text = _keyword_hits(text, stems) > 0
        if by_category or by_text:
            hits += 1
            matched.append(interest)
    reason = f"интересы: {', '.join(matched)}" if matched else None
    return _interest_score(hits, len(interests)), reason


def _interest_score(hits: int, chosen: int) -> float:
    """Saturating in the number of hits, judged against what was asked.

    Raw saturation is 1 hit ~55%, 2 ~80%, 3+ ~90%. It is normalised to the
    number of interests chosen, capped at three, so that matching everything
    that was asked is 100% and choosing many interests never dilutes the score.
    """

    if hits <= 0 or chosen <= 0:
        return 0.0
    ceiling = 1.0 - math.exp(-0.8 * min(chosen, 3))
    return min(1.0, (1.0 - math.exp(-0.8 * hits)) / ceiling)


def _trip_type_score(
    trip_type: TripType | None,
    text: str,
    categories: frozenset[str] = frozenset(),
) -> tuple[float, str | None]:
    if trip_type is None:
        return 0.5, None
    overlap = len(categories & TRIP_TYPE_CATEGORIES[trip_type])
    hits = _keyword_hits(text, _TRIP_KEYWORDS[trip_type])
    if overlap >= 2 or hits >= 2:
        return 1.0, f"тип «{trip_type}»"
    if overlap == 1 or hits == 1:
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


def difficulty_fit(
    preferences: UserPreferenceSignals, candidate: RouteMatchCandidate
) -> ProfileFit | None:
    return profile_fit(
        preferred_difficulty=preferences.difficulty,
        preferred_transport=preferences.transport,
        level=candidate.difficulty_level or level_from_legacy(candidate.difficulty),
        walk_level=candidate.walk_level,
    )


def _preference_score(
    preferences: UserPreferenceSignals | None,
    candidate: RouteMatchCandidate,
) -> tuple[float, str | None] | None:
    """None when the profile says nothing that applies to this route."""

    if preferences is None:
        return None
    parts: list[float] = []
    reasons: list[str] = []
    if preferences.categories:
        preferred_categories: set[str] = set()
        for category in preferences.categories:
            preferred_categories.update(categories_for_interest(category))
        overlap = preferred_categories & set(candidate.category_slugs)
        category_score = min(1.0, len(overlap) / max(1, min(2, len(preferred_categories))))
        parts.append(category_score)
        if overlap:
            reasons.append("совпадает с предпочтениями")
    fit = difficulty_fit(preferences, candidate)
    if fit is not None:
        parts.append(fit.score)
        if fit.reason:
            reasons.append(fit.reason)
    if preferences.travels_with_kids:
        parts.append(
            1.0
            if candidate.suitable_for_children is True
            else 0.15
            if candidate.suitable_for_children is False
            else 0.5
        )
    if preferences.travels_with_pets:
        parts.append(
            1.0
            if candidate.pets_allowed is True
            else 0.15
            if candidate.pets_allowed is False
            else 0.5
        )
    if not parts:
        return None
    return sum(parts) / len(parts), ", ".join(dict.fromkeys(reasons)) or None


@dataclass(frozen=True, slots=True)
class _Part:
    """One weighted signal. Unknown or unrequested signals never enter the score."""

    weight: float
    score: float
    reason: str | None = None
    requested: bool = True
    known: bool = True
    mismatch: str | None = None
    # Lower = more important when a card shows only one mismatch.
    rank: int = 99


_START_FIELDS = frozenset({"city", "start_query", "start_locality_id", "start_place_id"})


# A route counts as «по Евпатории» when at least this share of its stops is
# there: a three-day tour of Crimea with one stop in town does not.
#: The share of a route's stops inside a named area for it to be «there».
MIN_AREA_SHARE = 0.5
#: A stop this close to a named town's centre is in that town (BACKEND-68).
TOWN_RADIUS_KM = 6.0


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance between two lng/lat points, km."""
    lng1, lat1 = map(math.radians, a)
    lng2, lat2 = map(math.radians, b)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _named(town: str, place: str) -> bool:
    """«Форос» names «Форосский парк»: the town's stem starts a word of the
    place name. A short stem is not enough to match inside another word."""
    stem = town[: max(4, len(town) - 2)] if len(town) > 5 else town
    return re.search(rf"(?<![а-яёa-z]){re.escape(stem)}", place.casefold()) is not None


MIN_NAMED_TOWN_SHARE = 0.25


def _explicit(
    field: str,
    confirmed_fields: Sequence[str] | None,
    explicit_fields: Sequence[str] | None,
) -> bool:
    """Whether a value with a form default was actually chosen by the person.

    Chat tells us via ``confirmed_fields``; an updated form via
    ``explicit_fields``. An older client sends neither, so everything it
    sent counts as asked (the previous behaviour).
    """

    if confirmed_fields is not None:
        return field in confirmed_fields
    if explicit_fields is not None:
        return field in explicit_fields
    return True


def _location_requested(
    params: RouteMatchParamsIn,
    confirmed_fields: Sequence[str] | None,
) -> bool:
    if params.search_area:
        return (
            confirmed_fields is None
            or "search_area" in confirmed_fields
            or "city" in confirmed_fields
        )
    start = params.effective_start_query
    if not start or start.casefold() == "крым":
        return False
    return confirmed_fields is None or bool(_START_FIELDS & set(confirmed_fields))


def requested_signal_count(
    params: RouteMatchParamsIn,
    *,
    confirmed_fields: Sequence[str] | None = None,
    explicit_fields: Sequence[str] | None = None,
) -> int:
    """How many parameters the person actually asked for (candidate independent)."""

    flags = (
        _location_requested(params, confirmed_fields),
        bool(params.preferred_localities),
        _explicit("duration", confirmed_fields, explicit_fields),
        bool(params.interests),
        params.trip_type is not None,
        _explicit("pace", confirmed_fields, explicit_fields),
        params.transport_mode is not None,
        bool(params.season),
        params.with_children is True,
        params.with_pets is True,
        params.avoid_crowds is True,
        params.budget_amount is not None,
        params.paid_ok is False,
    )
    return sum(flags)


def _excluded(
    candidate: RouteMatchCandidate, reason: str, mismatch: str, signals: int
) -> ScoredMatch:
    return ScoredMatch(
        candidate=candidate,
        score=0,
        reasons=(reason,),
        mismatches=(mismatch,),
        requested_signals=signals,
        excluded=True,
    )


def score_candidate(
    params: RouteMatchParamsIn,
    candidate: RouteMatchCandidate,
    preferences: UserPreferenceSignals | None = None,
    *,
    confirmed_fields: list[str] | None = None,
    explicit_fields: list[str] | None = None,
    town_points: Mapping[str, tuple[float, float]] | None = None,
) -> ScoredMatch:
    """[town_points]: lng/lat of the named towns, from the localities
    directory, so a stop counts as in Симеиз by distance even when its place
    is filed under the municipality (Ялта) — BACKEND-68."""
    signals = requested_signal_count(
        params, confirmed_fields=confirmed_fields, explicit_fields=explicit_fields
    )
    # Safety is the one thing that stays a hard exclusion (D2).
    if params.with_children is True and candidate.suitable_for_children is False:
        return _excluded(candidate, "не подходит по ограничениям", "не подходит для детей", signals)
    if params.with_pets is True and candidate.pets_allowed is False:
        return _excluded(candidate, "не подходит по ограничениям", "нельзя с питомцами", signals)

    text = _haystack(candidate)
    parts: list[_Part] = []
    depth = 0.0

    # --- start / area
    c_score, c_reason = _location_score(params.effective_start_query, candidate)
    scope_text = " ".join(candidate.locality_names).casefold()
    if params.search_area:
        from tourism_backend.modules.route_builder.application.discovery import (
            area_bounds,
            area_localities,
        )

        locations = area_localities(params.search_area)
        bounds = area_bounds(params.search_area)
        # Structured locality wins over a marketing title mentioning another area.
        # Most of the route is in the area, not one stop of it: a mountain
        # hike from Бахчисарай with one stop over Ялта is not «по ЮБК»
        # (BACKEND-68). The share also ranks equal scores.
        in_area: list[bool] = (
            [
                bounds[0] <= lng <= bounds[2] and bounds[1] <= lat <= bounds[3]
                for lng, lat in candidate.stop_coordinates
            ]
            if bounds
            else []
        )
        inside_share = sum(in_area) / len(in_area) if in_area else 0.0
        inside_bounds = bool(bounds) and inside_share >= MIN_AREA_SHARE
        if bounds:
            depth += 2.0 * inside_share
        if (
            locations
            and not bounds
            and not any(name.casefold() in scope_text for name in locations)
        ) or (bounds and not inside_bounds):
            return _excluded(candidate, "вне выбранного района", "вне выбранного района", signals)
        c_score, c_reason = 1.0, f"район поиска: {params.search_area}"
    if _location_requested(params, confirmed_fields):
        start = (params.effective_start_query or "").casefold()
        first = (candidate.stop_localities[0] or "") if candidate.stop_localities else ""
        # Starts right there: above a route that only passes by (BACKEND-68).
        if start and first and (start in first.casefold() or first.casefold() in start):
            depth += 1.0
        parts.append(
            _Part(
                0.32,
                c_score,
                c_reason,
                mismatch="старт не совпал" if c_score < 0.3 else None,
                rank=2,
            )
        )
    if params.preferred_localities:
        wanted = [name.casefold() for name in params.preferred_localities]
        # «Маршруты по Евпатории»: with no wider area named, the towns are
        # the area, and a route elsewhere is not an answer (BACKEND-64).
        # «Посёлки вроде Фороса» inside a named area stay a soft wish.
        whole_region = (params.search_area or "").casefold() in {"крым", "crimea", "весь крым"}
        # A named area that is not all Crimea makes the towns examples:
        # «вдоль ЮБК, по типу Симеиза, Фороса» (BACKEND-68).
        towns_are_the_area = whole_region or (
            not params.search_area and not _location_requested(params, confirmed_fields)
        )
        points = [point for name, point in (town_points or {}).items() if name.casefold() in wanted]

        def near_a_town(coordinate: tuple[float, float] | None) -> bool:
            return coordinate is not None and any(
                _km(coordinate, point) <= TOWN_RADIUS_KM for point in points
            )

        if candidate.stop_localities:
            coordinates: list[tuple[float, float] | None] = list(candidate.stop_coordinates)
            coordinates += [None] * (len(candidate.stop_localities) - len(coordinates))
            inside = sum(
                1
                for locality, place, coordinate in zip(
                    candidate.stop_localities, candidate.place_names, coordinates, strict=False
                )
                if any(
                    name in (locality or "").casefold() or _named(name, place) for name in wanted
                )
                or near_a_town(coordinate)
            )
            share = inside / len(candidate.stop_localities)
        else:
            share = float(
                any(
                    name in scope_text
                    or any(name in place.casefold() for place in candidate.place_names)
                    for name in wanted
                )
            )
        depth += 2.0 * share
        if towns_are_the_area and share < MIN_NAMED_TOWN_SHARE:
            return _excluded(candidate, "вне названного города", "вне названного города", signals)
        has_preferred = share > 0
        parts.append(
            _Part(
                0.32 if towns_are_the_area else 0.22,
                (0.4 + 0.6 * share if towns_are_the_area else 1.0) if has_preferred else 0.05,
                "есть места из ваших пожеланий" if has_preferred else None,
                mismatch=None if has_preferred else "нет мест из ваших пожеланий",
                rank=6,
            )
        )

    # --- duration and pace: only when the person really chose them
    if _explicit("duration", confirmed_fields, explicit_fields):
        d_score, d_reason = _duration_score(params.duration, candidate.estimated_duration_minutes)
        known = bool(
            candidate.estimated_duration_minutes and candidate.estimated_duration_minutes > 0
        )
        parts.append(
            _Part(
                0.18,
                d_score,
                d_reason,
                known=known,
                mismatch="дольше, чем вы планировали" if known and d_score < 0.7 else None,
                rank=5,
            )
        )
    if params.interests:
        i_score, i_reason = _interests_score(
            params.interests,
            text,
            candidate.category_slugs,
            seaside=candidate.is_seaside,
        )
        wanted_kinds = frozenset().union(
            *(categories_for_interest(interest) for interest in params.interests)
        )
        depth += 0.25 * len(candidate.category_slugs & wanted_kinds)
        no_hits = i_reason is None
        parts.append(
            _Part(
                0.2,
                i_score,
                i_reason,
                # No categories and no text hit: absence of data, not of a match.
                known=bool(candidate.category_slugs) or candidate.is_seaside or not no_hits,
                mismatch="нет совпадений по интересам" if no_hits else None,
                rank=7,
            )
        )
    if params.trip_type is not None:
        t_score, t_reason = _trip_type_score(params.trip_type, text, candidate.category_slugs)
        parts.append(
            _Part(
                0.12,
                t_score,
                t_reason,
                known=bool(candidate.category_slugs),
                mismatch="не похоже на выбранный тип поездки" if t_score < 0.5 else None,
                rank=8,
            )
        )
    if _explicit("pace", confirmed_fields, explicit_fields):
        p_score, p_reason = _pace_score(params.pace, candidate.difficulty)
        parts.append(
            _Part(
                0.08,
                p_score,
                p_reason,
                known=bool(candidate.difficulty),
                mismatch="другой темп" if candidate.difficulty and p_score < 0.5 else None,
                rank=9,
            )
        )

    # --- transport: a mismatch is a penalty, not an exclusion (D2)
    transport_violation = False
    if params.transport_mode is not None:
        tr_score, tr_reason = _transport_score(params.transport_mode, candidate.transport_mode)
        actual = _normalize_transport(candidate.transport_mode)
        requested_mode = _normalize_transport(params.transport_mode)
        transport_violation = (
            actual is not None and requested_mode != "mixed" and actual != requested_mode
        )
        parts.append(
            _Part(
                # 0.05 let a car route top a «Пешком» search (BACKEND-42).
                TRANSPORT_WEIGHT,
                tr_score,
                tr_reason,
                known=actual is not None,
                mismatch="другой вид транспорта" if transport_violation else None,
                rank=3,
            )
        )
    # Without a chosen season the trip date, or today, says it (BACKEND-42):
    # Ай-Петри in winter or a beach in November is not a fit. A season
    # nobody chose weighs less than one they did.
    season = params.season or season_for(params.trip_start_date or today())
    chosen = bool(params.season)
    s_score, s_reason = _season_score(season, candidate.seasonality)
    parts.append(
        _Part(
            SEASON_WEIGHT if chosen else SEASON_WEIGHT_BY_DATE,
            s_score,
            s_reason,
            known=bool(candidate.seasonality),
            # Nobody chose the season by date: it moves the score but does not
            # decide whether the route matches what was asked.
            requested=chosen,
            mismatch=(
                ("не для выбранного сезона" if chosen else "не для этого времени года")
                if candidate.seasonality and s_score < 0.5
                else None
            ),
            rank=12,
        )
    )

    # --- party flags: False is excluded above; None means we simply do not know
    safety_unknown = False
    unknown_notes: list[str] = []
    if params.with_children is True:
        if candidate.suitable_for_children is True:
            parts.append(_Part(0.02, 1.0, "можно с детьми", rank=1))
        else:
            safety_unknown = True
            unknown_notes.append("нет данных о пригодности для детей")
            parts.append(_Part(0.02, 0.5, known=False, rank=1))
    if params.with_pets is True:
        if candidate.pets_allowed is True:
            parts.append(_Part(0.02, 0.9, "можно с питомцами", rank=1))
        else:
            safety_unknown = True
            unknown_notes.append("нет данных о том, можно ли с питомцами")
            parts.append(_Part(0.02, 0.5, known=False, rank=1))

    if params.avoid_crowds is True:
        crowd_value = {"low": 1.0, "medium": 0.5, "high": 0.0}.get(candidate.typical_crowding)
        parts.append(
            _Part(
                0.12,
                crowd_value if crowd_value is not None else 0.5,
                "обычно мало людей" if crowd_value == 1 else None,
                known=crowd_value is not None,
                mismatch="обычно много людей" if crowd_value == 0.0 else None,
                rank=11,
            )
        )
    if params.budget_amount is not None:
        known = candidate.price_min_amount is not None
        affordable = False
        if known:
            days = max(1, ((candidate.estimated_duration_minutes or 0) + 479) // 480)
            affordable = (candidate.price_min_amount or 0) <= params.budget_amount * days
        parts.append(
            _Part(
                0.12,
                1.0 if affordable else 0.0,
                "известная стоимость укладывается в бюджет" if affordable else None,
                known=known,
                mismatch="может выйти дороже бюджета" if known and not affordable else None,
                rank=10,
            )
        )

    paid_violation = params.paid_ok is False and (candidate.price_min_amount or 0) > 0

    # --- profile preferences: a soft extra signal, never "requested"
    preference = _preference_score(preferences, candidate)
    if preference is not None:
        parts.append(_Part(0.08, preference[0], preference[1], requested=False, rank=99))

    requested_weight = sum(part.weight for part in parts if part.requested)
    known_requested = sum(part.weight for part in parts if part.requested and part.known)
    scoring = [part for part in parts if part.known]
    total_weight = sum(part.weight for part in scoring)
    partial = safety_unknown or (
        requested_weight > 0 and known_requested / requested_weight < MIN_KNOWN_SHARE
    )
    if total_weight <= 0:
        # Nothing known to judge by: a neutral value that never looks like a match.
        score = PARTIAL_DATA_CAP
        partial = True
    else:
        score = sum(part.weight * part.score for part in scoring) / total_weight

    violation = transport_violation or paid_violation or safety_unknown
    full_match = (
        not partial
        and not violation
        and known_requested == requested_weight
        and all(part.score >= 0.999 for part in parts if part.requested)
        and requested_weight > 0
    )
    if partial:
        score = min(score, PARTIAL_DATA_CAP)
    if transport_violation or paid_violation:
        score = min(score, PENALTY_CAP)

    mismatches: list[tuple[int, str]] = [
        (part.rank, part.mismatch) for part in parts if part.mismatch
    ]
    mismatches.extend((1, note) for note in unknown_notes)
    if paid_violation:
        mismatches.append((4, "есть платные места"))
    mismatches.sort(key=lambda item: item[0])
    reasons = [part.reason for part in parts if part.reason]
    return ScoredMatch(
        candidate=candidate,
        score=round(min(1.0, max(0.0, score)), 4),
        reasons=tuple(reasons[:6]),
        mismatches=tuple(dict.fromkeys(text for _, text in mismatches))[:6],
        partial_data=partial,
        has_violation=violation,
        full_match=full_match,
        requested_signals=signals,
        depth=depth,
    )


def match_percent(scored: ScoredMatch) -> int:
    """Shown percent: rounded down to 5, 100 only for a complete match (D9)."""

    percent = int(scored.score * 100) // 5 * 5
    if scored.full_match:
        return 100
    return min(95, percent)


def band_of(scored: ScoredMatch) -> str:
    return "ideal" if scored.score >= IDEAL_THRESHOLD else "close"


def select_hits(
    scored: list[ScoredMatch],
    *,
    tiebreak: Callable[[ScoredMatch], tuple[float, str]] | None = None,
) -> tuple[list[ScoredMatch], bool]:
    """One list by exact score (best first), floor and cap applied (D3, D18).

    ``tiebreak`` orders equal scores (rating first, then name). Returns the
    hits and whether to offer generating a route: no hit reaches "ideal".
    """

    def key(item: ScoredMatch) -> tuple[float, float, float, str]:
        rating, name = tiebreak(item) if tiebreak else (0.0, item.candidate.name)
        # Equal scores: the stronger answer, then the better rated (BACKEND-68).
        return (-item.score, -item.depth, -rating, name)

    eligible = [item for item in scored if not item.excluded and item.score >= MIN_MATCH_SCORE]
    hits = sorted(eligible, key=key)[:MAX_HITS]
    offer_generate = not any(item.score >= IDEAL_THRESHOLD for item in hits)
    return hits, offer_generate


def legacy_bands(hits: list[ScoredMatch]) -> tuple[list[ScoredMatch], list[ScoredMatch]]:
    """The two short arrays installed app versions expect (D15).

    Only routes without a violation: those clients cannot show why a route
    with the wrong transport is listed as "close".
    """

    clean = [item for item in hits if not item.has_violation]
    ideal = [item for item in clean if item.score >= IDEAL_THRESHOLD][:MAX_IDEAL]
    close = [item for item in clean if CLOSE_THRESHOLD <= item.score < IDEAL_THRESHOLD][:MAX_CLOSE]
    return ideal, close
