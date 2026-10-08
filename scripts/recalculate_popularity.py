#!/usr/bin/env python3
"""Nightly recalculation of place and route popularity (spec 19).

Dry-run by default: prints what would change. Run with ``--apply`` from a
scheduled maintenance job, once a day at night.

Examples:
  uv run python scripts/recalculate_popularity.py
  uv run python scripts/recalculate_popularity.py --apply
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.popularity import recalc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the result; without this flag the command is a dry-run",
    )
    args = parser.parse_args()
    engine = create_engine(get_settings().database_url_sync)
    with Session(engine) as session:
        report = recalc.compute(session)
        if args.apply:
            recalc.apply(session, report)
            session.commit()
    mode = "applied" if args.apply else "dry-run"
    print(
        f"[{mode}] eligible users: {report.eligible_users}; "
        f"routes: {len(report.routes)} scored of {report.routes_published} published, "
        f"{report.popular_routes} popular; "
        f"places: {len(report.places)} scored of {report.places_published} published, "
        f"{report.popular_places} popular"
    )


if __name__ == "__main__":
    main()
