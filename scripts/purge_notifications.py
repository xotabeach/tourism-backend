#!/usr/bin/env python3
"""Bounded cleanup of old notifications.

Dry-run by default. Read notifications older than 90 days and any older than
180 days are removed, in batches, so a big table never holds one long
transaction. Run with ``--apply`` from a scheduled maintenance job.

Examples:
  uv run python scripts/purge_notifications.py
  uv run python scripts/purge_notifications.py --apply
"""

from __future__ import annotations

import argparse
import time

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.notifications.application.retention import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MAX_BATCHES_PER_RUN,
    any_cutoff,
    expired_notification_ids,
    read_cutoff,
)
from tourism_backend.modules.notifications.infrastructure.models import Notification


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=MAX_BATCHES_PER_RUN)
    parser.add_argument("--pause", type=float, default=0.2, help="seconds between batches")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete expired rows; without this flag the command is a dry-run",
    )
    args = parser.parse_args()
    if not 1 <= args.batch_size <= MAX_BATCH_SIZE:
        raise SystemExit(f"batch-size must be between 1 and {MAX_BATCH_SIZE}")
    if not 1 <= args.max_batches <= MAX_BATCHES_PER_RUN:
        raise SystemExit(f"max-batches must be between 1 and {MAX_BATCHES_PER_RUN}")
    read_before = read_cutoff()
    any_before = any_cutoff()
    engine = create_engine(get_settings().database_url_sync)
    scanned = 0
    deleted = 0
    batches = 0
    with Session(engine) as session:
        for _ in range(args.max_batches):
            query = expired_notification_ids(
                read_before=read_before, any_before=any_before, limit=args.batch_size
            )
            ids = list(session.scalars(query))
            batches += 1
            scanned += len(ids)
            if not ids or not args.apply:
                break
            session.execute(delete(Notification).where(Notification.id.in_(ids)))
            session.commit()
            deleted += len(ids)
            if len(ids) < args.batch_size:
                break
            time.sleep(args.pause)
    mode = "applied" if args.apply else "dry-run"
    print(
        f"notification_retention[{mode}]: read_before={read_before.isoformat()} "
        f"any_before={any_before.isoformat()} scanned={scanned} deleted={deleted} "
        f"batches={batches}"
    )
    if not args.apply and scanned >= args.batch_size:
        print(f"note: dry-run counts at most one batch ({args.batch_size}) of expired rows")


if __name__ == "__main__":
    main()
