"""Transactional source-pack import. Caller explicitly commits or rolls back."""

import hashlib
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.support.infrastructure.help_catalog import HelpCatalog
from tourism_backend.modules.support.infrastructure.help_models import SupportHelpRevision


async def import_help(
    session: AsyncSession,
    catalog: HelpCatalog,
    *,
    review_until: datetime | None = None,
) -> int:
    manifest = catalog.manifest
    now = datetime.now(UTC)
    publishing = manifest.status == "published"
    if publishing and (review_until is None or review_until <= now):
        raise ValueError("Publishing requires an explicit future review deadline")
    # Serialize imports, including first inserts where no row exists to lock.
    await session.execute(text("SELECT pg_advisory_xact_lock(8312056)"))
    changed = 0
    for article in catalog.articles:
        spec = article.spec
        scope = (
            SupportHelpRevision.article_id == spec.id,
            SupportHelpRevision.app_version == manifest.target_app_version,
            SupportHelpRevision.language == manifest.language,
        )
        rows = list((await session.scalars(select(SupportHelpRevision).where(*scope))).all())
        row = next((r for r in rows if r.revision == spec.revision), None)
        fingerprint = hashlib.sha256(
            (spec.model_dump_json() + article.content_hash).encode()
        ).hexdigest()
        if row is not None and row.content_hash != fingerprint:
            raise ValueError(f"Article {spec.id}: content changed; increment revision")
        if row is not None and row.status == manifest.status:
            continue  # No implicit renewal of a published/expired edition.
        if row is not None and row.status == "withdrawn":
            raise ValueError(f"Article {spec.id}: withdrawn revision cannot be restored")
        if row is not None and row.status == "published" and manifest.status == "draft":
            raise ValueError("Cannot turn published help into a draft; explicitly withdraw it")
        if publishing and any(r.revision > spec.revision and r.status != "draft" for r in rows):
            raise ValueError("Cannot publish an older revision over newer history")
        if row is None:
            row = SupportHelpRevision(
                id=uuid5(
                    NAMESPACE_URL,
                    f"support-help:{spec.id}:{spec.revision}:{manifest.target_app_version}:ru",
                ),
                article_id=spec.id,
                revision=spec.revision,
                app_version=manifest.target_app_version,
                language=manifest.language,
                category=spec.category,
                faq_id=spec.faq_id,
                title=spec.title,
                question=spec.question,
                body=article.plain_text,
                content_hash=fingerprint,
                status="draft",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        if publishing:
            for previous in rows:
                if previous.id != row.id and previous.status == "published":
                    previous.status = "withdrawn"
                    previous.updated_at = now
            await session.flush()  # Free the single-current-edition unique index first.
            row.approved_by = manifest.approved_by
            row.published_at = now
            row.review_until = review_until
        row.status = manifest.status
        row.updated_at = now
        changed += 1
    await session.flush()
    return changed
