#!/usr/bin/env python3
"""Count every route's cost and price level once (BACKEND-56, spec 33).

After that the backend keeps them current by itself: a rebuilt route, a
place's price saved on «Цены мест» and the rules saved on «Стоимость
маршрутов» each recompute what they touch. This is for the first fill after
the deploy and for a check by hand. Dry-run by default.

Examples:
  uv run python scripts/recalculate_route_costs.py
  uv run python scripts/recalculate_route_costs.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import tourism_backend.main  # noqa: F401  (registers every model's table)
from tourism_backend.config import get_settings
from tourism_backend.modules.routes.application.route_cost import load_rules, refresh_costs
from tourism_backend.modules.routes.infrastructure.models import Route


async def run(apply: bool) -> None:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            count = await refresh_costs(session, rules=await load_rules(session))
            published = list(
                (
                    await session.execute(
                        select(Route.cost, Route.price_level).where(
                            Route.publication_status == "published"
                        )
                    )
                ).all()
            )
            if apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await engine.dispose()
    known = sum(1 for cost, _ in published if cost and cost.get("known"))
    levels = Counter(level for _, level in published if level is not None)
    mode = "applied" if apply else "dry-run"
    print(
        f"[{mode}] routes counted: {count}; published: {len(published)}, "
        f"cost known: {known}, by level: {dict(sorted(levels.items()))}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the result; without this flag the command is a dry-run",
    )
    asyncio.run(run(parser.parse_args().apply))


if __name__ == "__main__":
    main()
