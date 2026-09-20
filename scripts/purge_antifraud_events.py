#!/usr/bin/env python3
"""Bounded cleanup for old anti-fraud violation events.

The command is dry-run by default. Events of a run with an open points hold
(or one decided inside the retention window) are kept. Holds older than the
configured overdue threshold are reported as a warning so the review queue
does not silently rot. Run with ``--apply`` from a scheduled maintenance job.

Examples:
  uv run python scripts/purge_antifraud_events.py
  uv run python scripts/purge_antifraud_events.py --days 90 --apply
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.route_execution.application.antifraud_retention import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_RETENTION_DAYS,
    MAX_BATCH_SIZE,
    MAX_BATCHES_PER_RUN,
    MAX_RETENTION_DAYS,
    eligible_violation_ids,
    retention_cutoff,
)
from tourism_backend.modules.route_execution.infrastructure.models import (
    RoutePaceViolation,
    RoutePointsHold,
)

OVERDUE_HOLD_DAYS = 7


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"retain at least this many days (1..{MAX_RETENTION_DAYS})",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=MAX_BATCHES_PER_RUN)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete eligible rows; without this flag the command is a dry-run",
    )
    args = parser.parse_args()
    if not 1 <= args.days <= MAX_RETENTION_DAYS:
        raise SystemExit(f"days must be between 1 and {MAX_RETENTION_DAYS}")
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        raise SystemExit(f"batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if not 1 <= args.max_batches <= MAX_BATCHES_PER_RUN:
        raise SystemExit(f"max-batches must be between 1 and {MAX_BATCHES_PER_RUN}")
    cutoff = retention_cutoff(args.days)
    engine = create_engine(get_settings().database_url_sync)
    scanned = 0
    deleted = 0
    batches = 0
    with Session(engine) as session:
        for _ in range(args.max_batches):
            ids = list(
                session.scalars(eligible_violation_ids(cutoff=cutoff, limit=args.batch_size))
            )
            batches += 1
            scanned += len(ids)
            if not ids or not args.apply:
                break
            session.execute(delete(RoutePaceViolation).where(RoutePaceViolation.id.in_(ids)))
            deleted += len(ids)
            session.commit()
            if len(ids) < args.batch_size:
                break
        overdue_before = datetime.now(UTC) - timedelta(days=OVERDUE_HOLD_DAYS)
        overdue = int(
            session.scalar(
                select(func.count())
                .select_from(RoutePointsHold)
                .where(
                    RoutePointsHold.status == "held",
                    RoutePointsHold.created_at < overdue_before,
                )
            )
            or 0
        )
    mode = "applied" if args.apply else "dry-run"
    print(
        f"antifraud_event_retention[{mode}]: cutoff={cutoff.isoformat()} "
        f"scanned={scanned} deleted={deleted} batches={batches}"
    )
    if overdue:
        print(f"WARNING: {overdue} points hold(s) waiting longer than {OVERDUE_HOLD_DAYS} days")


if __name__ == "__main__":
    main()
