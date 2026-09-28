#!/usr/bin/env python3
"""Route difficulty by the estimate (spec 17, sections 4 and 9).

Dry-run by default: prints «was / now / why» for every route and changes
nothing. ``--switch-legacy`` moves routes rated before spec 17 to the
estimate (only after the owner has seen the table); ``--apply`` alone
recomputes every route under the current formula and lists what moved.
Old ratings go to the admin audit.

  uv run python scripts/recalculate_route_difficulty.py
  uv run python scripts/recalculate_route_difficulty.py --switch-legacy --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys

from sqlalchemy import select

import tourism_backend.main  # noqa: F401  (every model, for the mapper)
from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.routes.application.structure import refresh_route_structure
from tourism_backend.modules.routes.infrastructure.models import Route

_REASON_WORDS = {
    "walk_effort": "пешком {effort_km} эф.км ({km} км, +{ascent_m} м)",
    "trail": "тропа {grade} {meters} м",
    "steep": "уклон {degrees}°",
    "drive_hours": "за рулём {hours} ч",
    "unpaved": "грунтовка {meters} м",
    "offroad": "бездорожье {meters} м",
    "serpentine": "серпантин {meters} м",
    "walk_and_drive": "и идти, и ехать непросто",
    "long_day": "долгий день {minutes} мин",
    "multi_day": "{days} дней",
    "low_data": "примерно",
}


def _why(route: Route) -> str:
    breakdown = (route.accessibility or {}).get("difficulty") or {}
    words = []
    for reason in breakdown.get("reasons") or []:
        template = _REASON_WORDS.get(str(reason.get("code")))
        if template:
            try:
                words.append(template.format(**reason))
            except (KeyError, ValueError):
                words.append(str(reason.get("code")))
    return "; ".join(words)


async def _main(*, apply: bool, switch_legacy: bool) -> None:
    engine = create_engine(get_settings())
    writer = csv.writer(sys.stdout, delimiter="\t")
    writer.writerow(["маршрут", "источник", "было", "расчёт", "награда", "станет", "почему"])
    moved = 0
    try:
        async with create_session_factory(engine)() as session:
            routes = (
                await session.scalars(
                    select(Route)
                    .where(Route.lifecycle_status != "deleted")
                    .order_by(Route.source, Route.name)
                )
            ).all()
            for route in routes:
                was = route.difficulty_level
                was_manual = (route.difficulty_manual, route.difficulty_manual_by)
                if switch_legacy and route.difficulty_manual_by == "legacy":
                    route.difficulty_manual = None
                    route.difficulty_manual_by = None
                await refresh_route_structure(session, route)
                now = route.difficulty_level
                writer.writerow(
                    [
                        route.name,
                        route.source,
                        was,
                        f"{route.difficulty_auto} ({route.difficulty_confidence})",
                        route.difficulty_reward,
                        now,
                        _why(route),
                    ]
                )
                if was != now:
                    moved += 1
                    if apply:
                        await record_audit(
                            session,
                            actor_id=None,
                            action="route.difficulty.recalculate",
                            entity_type="route",
                            entity_id=str(route.id),
                            metadata={
                                "was": was,
                                "now": now,
                                "manual_was": list(was_manual),
                                "estimate": route.difficulty_auto,
                                "formula_version": route.difficulty_formula_version,
                            },
                        )
            if apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await engine.dispose()
    verb = "changed" if apply else "would change"
    print(f"\n{len(routes)} routes, {moved} {verb} their shown difficulty", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--switch-legacy", action="store_true")
    args = parser.parse_args()
    asyncio.run(_main(apply=args.apply, switch_legacy=args.switch_legacy))
