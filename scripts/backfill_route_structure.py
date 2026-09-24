#!/usr/bin/env python3
"""Fill days and segments for every route (spec 14, step 0).

Each route gets one day and one segment per leg in its own mode, built from
the legs its routing already has; the router is not called. A run start does
the same for its route, so this only saves the first start of each route the
work, and lets the admin and the API see the rows at once.

Dry-run by default: prints every route of more than one day and the counts
per base mode.

  uv run python scripts/backfill_route_structure.py
  uv run python scripts/backfill_route_structure.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import select

from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.admin.infrastructure import models as _admin_models  # noqa: F401
from tourism_backend.modules.favorites.infrastructure import models as _favorites  # noqa: F401
from tourism_backend.modules.geography.infrastructure import models as _geography  # noqa: F401
from tourism_backend.modules.identity.infrastructure import models as _identity  # noqa: F401
from tourism_backend.modules.places.infrastructure import models as _places  # noqa: F401
from tourism_backend.modules.recommendations.infrastructure import (
    models as _recommendations,  # noqa: F401
)
from tourism_backend.modules.route_execution.infrastructure import (
    models as _route_execution,  # noqa: F401
)
from tourism_backend.modules.routes.application.structure import refresh_route_structure
from tourism_backend.modules.routes.infrastructure.models import Route
from tourism_backend.modules.subscriptions.infrastructure import (
    models as _subscription_models,  # noqa: F401
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    modes: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    try:
        async with factory() as session:
            routes = list((await session.scalars(select(Route).order_by(Route.created_at))).all())
            for route in routes:
                segments, days = await refresh_route_structure(session, route)
                modes[route.base_mode] += 1
                origins.update(segment.origin for segment in segments)
                if len(days) > 1 or any(day.overloaded for day in days):
                    overloaded = sum(day.overloaded for day in days)
                    print(
                        f"  {route.name}: {len(days)} дн."
                        + (f", перегружено {overloaded}" if overloaded else "")
                    )
            if args.apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await engine.dispose()
    print(
        f"routes={sum(modes.values())} base_modes={dict(modes)} "
        f"segments={dict(origins)} mode={'apply' if args.apply else 'dry-run'}"
    )


if __name__ == "__main__":
    asyncio.run(main())
