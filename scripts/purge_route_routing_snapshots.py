#!/usr/bin/env python3
"""Bounded cleanup for old, unreferenced route-routing snapshots.

The command is dry-run by default. It never removes the newest revision for a
route or a snapshot referenced by ``route_executions``. Run with ``--apply``
from a scheduled maintenance job after reviewing the reported count.

Examples:
  uv run python scripts/purge_route_routing_snapshots.py
  uv run python scripts/purge_route_routing_snapshots.py --days 730 --apply
"""

from __future__ import annotations

import argparse

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.route_execution.application.retention import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_RETENTION_DAYS,
    MAX_BATCH_SIZE,
    MAX_BATCHES_PER_RUN,
    MAX_RETENTION_DAYS,
    eligible_snapshot_ids,
    retention_cutoff,
)
from tourism_backend.modules.route_execution.infrastructure.models import RouteRoutingSnapshot


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
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        raise SystemExit(f"batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if not 1 <= args.max_batches <= MAX_BATCHES_PER_RUN:
        raise SystemExit(f"max-batches must be between 1 and {MAX_BATCHES_PER_RUN}")
    cutoff = retention_cutoff(args.days)
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    scanned = 0
    deleted = 0
    batches = 0
    with Session(engine) as session:
        for _ in range(args.max_batches):
            ids = list(session.scalars(eligible_snapshot_ids(cutoff=cutoff, limit=args.batch_size)))
            batches += 1
            scanned += len(ids)
            if not ids or not args.apply:
                break
            result = session.execute(
                delete(RouteRoutingSnapshot).where(RouteRoutingSnapshot.id.in_(ids))
            )
            deleted += int(result.rowcount or 0)
            if len(ids) < args.batch_size:
                break
        if args.apply:
            session.commit()
    mode = "applied" if args.apply else "dry-run"
    print(
        f"route_routing_snapshot_retention[{mode}]: cutoff={cutoff.isoformat()} "
        f"scanned={scanned} deleted={deleted} batches={batches}"
    )


if __name__ == "__main__":
    main()
