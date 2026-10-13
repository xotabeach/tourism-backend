"""«Горный маршрут»: warn before the start, with a margin (BACKEND-39).

Spec 15, D8 and D21. A route is mountainous when a moderator or an editor
says so, or by the numbers: a high difficulty or a large ascent. The
difficulty estimate is low in places (spec 17), so when it is not reliable
(``difficulty_confidence`` low, no ascent known) a route whose stops are
mostly outside settlements counts as mountainous too. The two thresholds
live in the admin (``rt_`` runtime settings), bounded like the anti-fraud
ones so a typo cannot switch the warning off.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import (
    Route,
    RouteSegment,
    RouteStop,
)
from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

_logger = logging.getLogger("tourism_backend.routes")

KEY_PREFIX = "rt_mountain_"
KEY_MIN_LEVEL = "rt_mountain_min_level"
KEY_MIN_ASCENT = "rt_mountain_min_ascent_m"

#: key → (default, minimum, maximum)
_SPECS: dict[str, tuple[int, int, int]] = {
    KEY_MIN_LEVEL: (4, 2, 5),
    KEY_MIN_ASCENT: (500, 100, 3000),
}
ALL_KEYS = frozenset(_SPECS)

#: Below this share of stops in a settlement a route is «вне населённых
#: пунктов» for the unreliable case.
_OUTSIDE_SETTLEMENT_SHARE = 0.5

MountainSource = Literal["manual", "level", "ascent", "unreliable", "none"]


@dataclass(frozen=True, slots=True)
class MountainRules:
    min_level: int = 4
    min_ascent_m: int = 500


@dataclass(frozen=True, slots=True)
class MountainStatus:
    is_mountain: bool
    #: Why: the moderator's choice, the difficulty, the ascent, or the margin
    #: for an unreliable estimate.
    source: MountainSource


def validate_setting(key: str, value: str) -> str:
    """An admin-entered value normalised, or ``ValueError``."""
    default, low, high = _SPECS[key]
    del default
    text = value.strip()
    if not text.lstrip("-").isdigit():
        raise ValueError("Нужно целое число.")
    number = int(text)
    if not low <= number <= high:
        raise ValueError(f"Допустимо от {low} до {high}.")
    return str(number)


def parse_rules(raw: Mapping[str, str]) -> MountainRules:
    values: dict[str, int] = {}
    for key, (default, _low, _high) in _SPECS.items():
        stored = raw.get(key)
        if stored is None:
            values[key] = default
            continue
        try:
            values[key] = int(validate_setting(key, stored))
        except ValueError:
            _logger.warning("mountain_setting_invalid key=%s value=%r", key, stored)
            values[key] = default
    return MountainRules(min_level=values[KEY_MIN_LEVEL], min_ascent_m=values[KEY_MIN_ASCENT])


async def load_rules(session: AsyncSession) -> MountainRules:
    rows = (
        await session.execute(
            select(RuntimeSetting.key, RuntimeSetting.value).where(
                RuntimeSetting.key.like(f"{KEY_PREFIX}%")
            )
        )
    ).tuples()
    return parse_rules(dict(rows.all()))


def decide(
    *,
    manual: bool | None,
    level: int | None,
    ascent_m: int | None,
    reliable: bool,
    outside_settlements: bool,
    rules: MountainRules,
) -> MountainStatus:
    if manual is not None:
        return MountainStatus(manual, "manual")
    if level is not None and level >= rules.min_level:
        return MountainStatus(True, "level")
    if ascent_m is not None and ascent_m >= rules.min_ascent_m:
        return MountainStatus(True, "ascent")
    if (not reliable or ascent_m is None) and outside_settlements:
        return MountainStatus(True, "unreliable")
    return MountainStatus(False, "none")


async def mountain_status(
    session: AsyncSession, route: Route, rules: MountainRules | None = None
) -> MountainStatus:
    """Whether [route] is mountainous, and why."""
    rules = rules or await load_rules(session)
    if route.mountain_manual is not None:
        return MountainStatus(route.mountain_manual, "manual")
    ascent = await session.scalar(
        select(func.sum(RouteSegment.elevation_gain_meters)).where(
            RouteSegment.route_id == route.id
        )
    )
    total, in_town = (
        await session.execute(
            select(func.count(), func.count(Place.locality_id))
            .select_from(RouteStop)
            .join(Place, Place.id == RouteStop.place_id)
            .where(RouteStop.route_id == route.id)
        )
    ).one()
    outside = total > 0 and in_town / total < _OUTSIDE_SETTLEMENT_SHARE
    return decide(
        manual=None,
        level=route.difficulty_level,
        ascent_m=int(ascent) if ascent is not None else None,
        reliable=route.difficulty_confidence != "low",
        outside_settlements=outside,
        rules=rules,
    )
