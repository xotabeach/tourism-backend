"""The budget as one of four levels (BACKEND-57, spec 33 D1/D2/D13).

A person picks «Экономно», «Средне», «Комфорт» or «Без ограничений», per
person a day. Older builds still send a sum from the slider, and the chat
model may name one: the sum becomes a level here, by the same bounds the
routes' levels use. A sum of 0 stays «только бесплатное».

Matching reads only ``budget_level``. Building one's own route still picks
places by a sum, so a level gives it its upper bound; «Без ограничений» sets
no bound at all.
"""

from __future__ import annotations

from collections.abc import Sequence

from tourism_backend.modules.route_builder.application.schemas import RouteMatchParamsIn
from tourism_backend.modules.route_builder.application.scoring import NO_LIMIT_LEVEL
from tourism_backend.modules.routes.application.route_cost import LEVEL_NAMES, level_for


def budget_level_of(params: RouteMatchParamsIn, bounds: Sequence[int]) -> int | None:
    if params.budget_level is not None:
        return params.budget_level
    if params.budget_amount is None or params.budget_amount == 0:
        return None
    return level_for(params.budget_amount, bounds)


def normalize_budget(params: RouteMatchParamsIn, bounds: Sequence[int]) -> RouteMatchParamsIn:
    """Params with the level filled in and the sum matching it."""

    level = budget_level_of(params, bounds)
    updates: dict[str, object] = {"budget_level": level}
    if params.budget_level is None and params.budget_amount == 0 and params.paid_ok is None:
        updates["paid_ok"] = False
    if params.budget_level is not None:
        updates["budget_amount"] = (
            bounds[params.budget_level - 1] if params.budget_level < NO_LIMIT_LEVEL else None
        )
    return params.model_copy(update=updates)


def budget_level_label(level: int | None) -> str | None:
    if level is None or not 1 <= level <= len(LEVEL_NAMES):
        return None
    return LEVEL_NAMES[level - 1]
