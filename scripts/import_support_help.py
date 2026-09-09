"""Import only the explicit help pack; default is a read-only validation preview."""

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.support.application.help_import import import_help
from tourism_backend.modules.support.infrastructure.help_catalog import load_help_catalog


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/support_help"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--review-until", help="Required for publication: ISO datetime with timezone"
    )
    args = parser.parse_args()
    catalog = load_help_catalog(args.root)
    deadline = datetime.fromisoformat(args.review_until) if args.review_until else None
    if deadline is not None and deadline.tzinfo is None:
        parser.error("--review-until must include a timezone")
    print(f"{len(catalog.articles)} articles; status={catalog.manifest.status}.")
    if not args.apply:
        print("Preview only. No database writes; use --apply after review.")
        return
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session, session.begin():
            changed = await import_help(session, catalog, review_until=deadline)
        print(f"Applied {changed} revisions. No embeddings or generated answers requested.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
