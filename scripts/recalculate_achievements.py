#!/usr/bin/env python3
"""Dry-run by default. --apply writes; --reset requires a durable CSV backup.

Hourly: recalculate_achievements.py --apply --recent-hours 2
Historical: recalculate_achievements.py --apply --backfill
Initial reset: --apply --backfill --reset --backup-dir /opt/backups
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import text

from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.achievements.maintenance import (
    active_user_ids,
    backfill,
    recent_cutoff,
)
from tourism_backend.modules.achievements.service import evaluate, push_grants
from tourism_backend.modules.admin.infrastructure import models as _admin_models  # noqa: F401
from tourism_backend.modules.subscriptions.infrastructure import (
    models as _subscription_models,  # noqa: F401
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--recent-hours", type=int, default=2)
    args = parser.parse_args()
    if args.reset and (not args.backfill or args.backup_dir is None):
        parser.error("--reset requires --backfill and --backup-dir")
    if not 1 <= args.recent_hours <= 48:
        parser.error("--recent-hours must be between 1 and 48")
    engine = create_engine(get_settings())
    factory = create_session_factory(engine)
    count = 0
    failed = 0
    try:
        async with factory() as lock:
            if not await lock.scalar(text("SELECT pg_try_advisory_lock(8100051)")):
                print("Another achievement maintenance job is running")
                return
            try:
                if args.backfill:
                    async with factory() as session:
                        count = await backfill(
                            session, reset=args.reset, backup_dir=args.backup_dir
                        )
                        if args.apply:
                            await session.commit()
                        else:
                            await session.rollback()
                else:
                    async with factory() as session:
                        users = list(
                            (
                                await session.scalars(
                                    active_user_ids(recent_cutoff(args.recent_hours))
                                )
                            ).all()
                        )
                    for user_id in users:
                        try:
                            async with factory() as session:
                                badges = await evaluate(session, user_id)
                                count += len(badges)
                                if args.apply:
                                    await session.commit()
                                    await push_grants(session, user_id, badges)
                                else:
                                    await session.rollback()
                        except Exception as exc:
                            failed += 1
                            print(f"Failed user {user_id}: {type(exc).__name__}")
                print(
                    f"achievements mode={'apply' if args.apply else 'dry-run'} "
                    f"grants={count} failures={failed}"
                )
            finally:
                await lock.execute(text("SELECT pg_advisory_unlock(8100051)"))
    finally:
        await engine.dispose()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
