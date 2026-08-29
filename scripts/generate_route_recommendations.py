#!/usr/bin/env python3
"""Fill today's Moscow-date recommendation decks for a bounded user page.

Dry-run by default. Does not call 2ГИС or other vendor APIs. Host cron should
run this shortly after 00:00 UTC (03:00 MSK); new users still get a lazy deck
on first ``GET /routes/recommendations/today``.

Examples:
  uv run python scripts/generate_route_recommendations.py
  uv run python scripts/generate_route_recommendations.py --limit 50 --apply
"""

from __future__ import annotations

import argparse
import asyncio

from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.favorites.infrastructure import models as _favorites_models
from tourism_backend.modules.geography.infrastructure import models as _geography_models
from tourism_backend.modules.identity.infrastructure import models as _identity_models
from tourism_backend.modules.places.infrastructure import models as _places_models
from tourism_backend.modules.recommendations.application.policy import MAX_USERS_PER_RUN
from tourism_backend.modules.recommendations.application.service import generate_missing_decks
from tourism_backend.modules.recommendations.infrastructure import models as _reco_models
from tourism_backend.modules.route_execution.infrastructure import models as _execution_models
from tourism_backend.modules.routes.infrastructure import models as _routes_models

_ = (
    _reco_models,
    _geography_models,
    _identity_models,
    _places_models,
    _routes_models,
    _favorites_models,
    _execution_models,
)


async def _run(limit: int, offset: int, apply: bool) -> dict[str, int]:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            return await generate_missing_decks(
                session,
                limit=limit,
                offset=offset,
                persist=apply,
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write missing decks; without this flag the command is a dry-run",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_USERS_PER_RUN:
        raise SystemExit(f"limit must be between 1 and {MAX_USERS_PER_RUN}")
    if args.offset < 0:
        raise SystemExit("offset must be >= 0")
    result = asyncio.run(_run(args.limit, args.offset, args.apply))
    mode = "applied" if args.apply else "dry-run"
    print(
        f"route_recommendations[{mode}]: scanned={result['scanned']} "
        f"already_present={result['already_present']} missing={result['missing']} "
        f"generated={result['generated']}"
    )


if __name__ == "__main__":
    main()
