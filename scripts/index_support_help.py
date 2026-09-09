"""Prepare the isolated MiniLM help index. Default: read-only publication preview."""

import argparse
import asyncio

from sqlalchemy import select

from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.knowledge.application.embedder import (
    SentenceTransformerEmbeddingProvider,
)
from tourism_backend.modules.support.application.help_semantic import (
    MINILM_ALIASES,
    MINILM_MODEL,
    index_help,
)
from tourism_backend.modules.support.application.help_visibility import visible_help
from tourism_backend.modules.support.infrastructure.help_models import SupportHelpRevision


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--model", choices=sorted(MINILM_ALIASES), default=MINILM_MODEL)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session, session.begin():
            ids = list(
                await session.scalars(
                    select(SupportHelpRevision.article_id)
                    .where(*visible_help(args.app_version))
                    .order_by(SupportHelpRevision.article_id)
                )
            )
            print(f"Published eligible articles for {args.app_version}: {len(ids)}")
            if not args.apply:
                print("Preview only: no index writes or model loading. Use --apply explicitly.")
                return
            changed = await index_help(
                session,
                app_version=args.app_version,
                provider=SentenceTransformerEmbeddingProvider(model_name=args.model),
            )
            print(f"Indexed {changed} revisions. No publication or generated answers.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
