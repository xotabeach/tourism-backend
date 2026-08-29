"""Versioned recommendation policy values.

Weights and caps are configuration, not UI magic. Changing them requires a
new ``RANKER_VERSION`` and review; clients must not supply their own.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

RANKER_VERSION = "v1"
DECK_SIZE = 16
SKIP_COOLDOWN_DAYS = 14
COMPLETED_COOLDOWN_DAYS = 30
MAX_CATEGORY_SHARE = 0.40
MAX_REGION_SHARE = 0.50
EXPLORATION_SHARE = 0.20
CANDIDATE_LIMIT = 200
MAX_USERS_PER_RUN = 200
FRESHNESS_HALF_LIFE_DAYS = 90.0

WEIGHT_EXPLICIT = 0.30
WEIGHT_CONTENT = 0.20
WEIGHT_CONTEXT = 0.10
WEIGHT_POPULARITY = 0.10
WEIGHT_FRESHNESS = 0.10
WEIGHT_COMPLETION = 0.10
WEIGHT_EXPLORATION = 0.10

FEEDBACK_ACTIONS = frozenset({"skip"})
EXCLUDED_QUALITY = frozenset({"unusable"})

# Crimea uses a fixed UTC+3 offset year-round.
MSK = timezone(timedelta(hours=3))

_DIFFICULTY_ALIASES: dict[str, frozenset[str]] = {
    "easy": frozenset({"easy", "лёгкий", "легкий", "1", "2"}),
    "moderate": frozenset({"moderate", "средний", "3"}),
    "hard": frozenset({"hard", "сложный", "4", "5", "difficult"}),
}

_SEASON_BY_MONTH = {
    12: "winter",
    1: "winter",
    2: "winter",
    3: "spring",
    4: "spring",
    5: "spring",
    6: "summer",
    7: "summer",
    8: "summer",
    9: "autumn",
    10: "autumn",
    11: "autumn",
}


def deck_date_for(as_of: datetime) -> date:
    """Calendar date of the daily deck in Moscow time."""

    aware = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    return aware.astimezone(MSK).date()


def season_for(as_of: datetime) -> str:
    aware = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    return _SEASON_BY_MONTH[aware.astimezone(MSK).month]


def normalize_difficulty(value: str | None) -> str | None:
    if value is None:
        return None
    needle = value.casefold().strip()
    if not needle:
        return None
    for canonical, aliases in _DIFFICULTY_ALIASES.items():
        if needle in aliases or needle == canonical:
            return canonical
    return needle
