"""Popularity arithmetic, free of the database (spec 19)."""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from tourism_backend.modules.popularity.policy import (
    BADGE_MIN_PEOPLE,
    BADGE_TOP_SHARE,
    DECAY_HALF_LIFE_DAYS,
    PLACE_APP_HALF_PEOPLE,
    WINDOW_DAYS,
)


@dataclass(frozen=True)
class Action:
    """Something one person did with a place or a route."""

    user_id: UUID
    entity_id: UUID
    weight: float
    at: datetime


@dataclass(frozen=True)
class EntityScore:
    raw: float
    people: int


def decay(age_days: float) -> float:
    """1.0 for today, 0.5 at the half-life, 0.0 outside the window."""

    if age_days < 0:
        age_days = 0.0
    if age_days > WINDOW_DAYS:
        return 0.0
    return float(0.5 ** (age_days / DECAY_HALF_LIFE_DAYS))


def entity_scores(
    actions: Iterable[Action],
    *,
    now: datetime,
    eligible_user_ids: Collection[UUID],
) -> dict[UUID, EntityScore]:
    """Raw score and head count per entity.

    One person is one vote: they give the weight of their strongest action,
    decayed by its age, never the sum of everything they did (D45). People
    outside ``eligible_user_ids`` do not count at all.
    """

    best: dict[tuple[UUID, UUID], float] = {}
    for action in actions:
        if action.user_id not in eligible_user_ids:
            continue
        age_days = (now - action.at) / timedelta(days=1)
        value = action.weight * decay(age_days)
        if value <= 0:
            continue
        key = (action.entity_id, action.user_id)
        if value > best.get(key, 0.0):
            best[key] = value
    totals: dict[UUID, float] = {}
    people: dict[UUID, int] = {}
    for (entity_id, _user_id), value in best.items():
        totals[entity_id] = totals.get(entity_id, 0.0) + value
        people[entity_id] = people.get(entity_id, 0) + 1
    return {
        entity_id: EntityScore(raw=totals[entity_id], people=people[entity_id])
        for entity_id in totals
    }


def percentile_scores(raw: Mapping[UUID, float]) -> dict[UUID, float]:
    """Spread positive raw scores over 0..100 by rank; ties share a value.

    A lone entity gets 100: it is the most popular one there is.
    """

    positive = {entity_id: value for entity_id, value in raw.items() if value > 0}
    if not positive:
        return {}
    values = sorted(positive.values())
    total = len(values)
    below: dict[float, int] = {}
    for index, value in enumerate(values):
        below.setdefault(value, index)
    counts: dict[float, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return {
        entity_id: round(100.0 * (below[value] + counts[value]) / total, 2)
        for entity_id, value in positive.items()
    }


def blend_place(*, external: float | None, app: float, people: int) -> float:
    """A place's final popularity from outside fame and the app's own numbers.

    The app's share grows with the number of people behind it. Without an
    external score (step 19-1 not run yet) the app's score stands alone.
    """

    if external is None:
        return round(app, 2)
    share = people / (people + PLACE_APP_HALF_PEOPLE) if people > 0 else 0.0
    return round(external * (1.0 - share) + app * share, 2)


def top_share(
    scores: Mapping[UUID, float],
    *,
    population: int,
    share: float = BADGE_TOP_SHARE,
) -> set[UUID]:
    """Ids in the top ``share`` of ``population`` by score, zero scores excluded.

    ``population`` is every published entity, scored or not, so the badge
    stays rare while only a handful have any score. Ties at the cut-off are
    all left out rather than picked at random.
    """

    quota = math.floor(population * share)
    if quota <= 0:
        return set()
    ranked = sorted(
        ((value, entity_id) for entity_id, value in scores.items() if value > 0),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if len(ranked) <= quota:
        return {entity_id for _value, entity_id in ranked}
    cutoff = ranked[quota][0]
    return {entity_id for value, entity_id in ranked[:quota] if value > cutoff}


def popular_routes(
    scores: Mapping[UUID, float],
    people: Mapping[UUID, int],
    *,
    population: int,
) -> set[UUID]:
    """Routes that earn «Популярное»: enough different people and the top tenth (D43)."""

    return {
        route_id
        for route_id in top_share(scores, population=population)
        if people.get(route_id, 0) >= BADGE_MIN_PEOPLE
    }


def popular_places(
    final: Mapping[UUID, float],
    people: Mapping[UUID, int],
    has_external: Collection[UUID],
    *,
    population: int,
) -> set[UUID]:
    """Places that earn «Популярное».

    A place with outside fame can carry the badge from day one. One known
    only from the app needs the same head count a route does, so a single
    opened card is never «popular».
    """

    return {
        place_id
        for place_id in top_share(final, population=population)
        if place_id in has_external or people.get(place_id, 0) >= BADGE_MIN_PEOPLE
    }
