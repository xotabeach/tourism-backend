#!/usr/bin/env python3
"""Aggregate app-version activity and drop per-user rows older than 35 days.

Dry-run by default; ``--apply`` aggregates every complete day first (so nothing
is lost), then deletes. Run daily from a scheduled maintenance job.
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.app_stats.application.common import moscow_today
from tourism_backend.modules.app_stats.application.retention import (
    aggregate_complete_days,
    purge_user_rows,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry-run")
    args = parser.parse_args()
    today = moscow_today()
    engine = create_engine(get_settings().database_url_sync)
    with Session(engine) as session:
        aggregated = aggregate_complete_days(session, today=today) if args.apply else 0
        purged = purge_user_rows(session, today=today, apply=args.apply)
        if args.apply:
            session.commit()
    mode = "applied" if args.apply else "dry-run"
    print(f"app_version_rows {mode}: aggregated={aggregated} purged={purged}")


if __name__ == "__main__":
    main()
