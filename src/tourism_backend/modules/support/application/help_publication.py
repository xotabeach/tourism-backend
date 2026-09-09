"""Publishing help revisions from the admin, with the review deadline attached.

Publication used to be a CLI run against the server: edit the manifest, ssh
in, `import_support_help.py --apply --review-until ...`. That put an
editorial decision behind a deploy-shaped procedure, and the approval it
recorded was whatever string the manifest happened to carry.

Here the approver is the operator performing the action and the deadline is
explicit, so `support_help_revisions` keeps a real answer to "who published
this, and until when is it considered current".
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.support.infrastructure.help_models import SupportHelpRevision

# A published article is a promise that someone checked it against the build.
# Long enough not to be busywork, short enough that the promise stays true.
DEFAULT_REVIEW_DAYS = 90


@dataclass
class PublicationOutcome:
    published: int = 0
    withdrawn: int = 0
    skipped: list[str] = field(default_factory=list)


async def publish_revisions(
    session: AsyncSession,
    *,
    revision_ids: Sequence[UUID],
    approved_by: str,
    review_until: datetime,
    now: datetime | None = None,
) -> PublicationOutcome:
    """Publishes [revision_ids], retiring whatever they replace.

    One article can have only one published revision per app version and
    language (`uq_support_help_current`), so publishing revision 2 has to
    withdraw revision 1 rather than sit beside it.
    """
    moment = now or datetime.now(UTC)
    outcome = PublicationOutcome()
    if review_until <= moment:
        raise ValueError("Срок проверки должен быть в будущем")
    if not approved_by.strip():
        raise ValueError("Публикация требует указания, кто её одобрил")

    rows = await _load(session, revision_ids)
    for row in rows:
        if row.status == "published":
            outcome.skipped.append(f"{row.article_id} r{row.revision}: уже опубликована")
            continue
        if row.status == "withdrawn":
            # Withdrawal is deliberate and finite: bringing the same text back
            # would erase the fact that it was pulled. A new revision is the
            # way back.
            outcome.skipped.append(
                f"{row.article_id} r{row.revision}: отозвана, нужна новая ревизия"
            )
            continue
        current = list(
            (
                await session.scalars(
                    select(SupportHelpRevision).where(
                        SupportHelpRevision.article_id == row.article_id,
                        SupportHelpRevision.app_version == row.app_version,
                        SupportHelpRevision.language == row.language,
                        SupportHelpRevision.status == "published",
                        SupportHelpRevision.id != row.id,
                    )
                )
            ).all()
        )
        for previous in current:
            previous.status = "withdrawn"
            previous.updated_at = moment
            outcome.withdrawn += 1
        # Flush before publishing: the unique index covers published rows, so
        # the replacement and its predecessor cannot be in it at once.
        if current:
            await session.flush()
        row.status = "published"
        row.approved_by = approved_by[:100]
        row.published_at = moment
        row.review_until = review_until
        row.updated_at = moment
        outcome.published += 1
        await session.flush()
    return outcome


async def withdraw_revisions(
    session: AsyncSession,
    *,
    revision_ids: Sequence[UUID],
    now: datetime | None = None,
) -> int:
    moment = now or datetime.now(UTC)
    changed = 0
    for row in await _load(session, revision_ids):
        if row.status == "withdrawn":
            continue
        row.status = "withdrawn"
        row.updated_at = moment
        changed += 1
    await session.flush()
    return changed


async def extend_review(
    session: AsyncSession,
    *,
    revision_ids: Sequence[UUID],
    review_until: datetime,
    now: datetime | None = None,
) -> int:
    """Moves the deadline out on articles that are still correct.

    Only for published ones: extending a draft's deadline says nothing, and
    an expired article silently reappearing is exactly what the deadline is
    supposed to prevent — re-checking it is the point.
    """
    moment = now or datetime.now(UTC)
    if review_until <= moment:
        raise ValueError("Срок проверки должен быть в будущем")
    changed = 0
    for row in await _load(session, revision_ids):
        if row.status != "published":
            continue
        row.review_until = review_until
        row.updated_at = moment
        changed += 1
    await session.flush()
    return changed


async def _load(session: AsyncSession, revision_ids: Sequence[UUID]) -> list[SupportHelpRevision]:
    if not revision_ids:
        return []
    return list(
        (
            await session.scalars(
                select(SupportHelpRevision)
                .where(SupportHelpRevision.id.in_(set(revision_ids)))
                .order_by(SupportHelpRevision.article_id, SupportHelpRevision.revision)
            )
        ).all()
    )
