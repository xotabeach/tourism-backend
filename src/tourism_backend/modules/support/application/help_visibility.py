"""One publication gate for lexical search, indexing, semantic search and reading."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tourism_backend.modules.support.infrastructure.help_models import SupportHelpRevision


def visible_help(app_version: str) -> tuple[ColumnElement[bool], ...]:
    now = datetime.now(UTC)
    return (
        SupportHelpRevision.app_version == app_version,
        SupportHelpRevision.language == "ru",
        SupportHelpRevision.status == "published",
        SupportHelpRevision.published_at <= now,
        SupportHelpRevision.review_until > now,
    )


def _version_key(value: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


async def resolve_help_version(session: AsyncSession, requested: str) -> str:
    """The help pack an app of ``requested`` version reads (BACKEND-29).

    Help is verified for one app version, but apps released after it must
    not lose help until the next pack: they read the newest published pack
    that is not newer than themselves. An app older than every pack, or an
    unparsable version, keeps its exact version (and finds nothing).
    """
    wanted = _version_key(requested)
    if wanted is None:
        return requested
    now = datetime.now(UTC)
    versions = (
        await session.scalars(
            select(SupportHelpRevision.app_version)
            .where(
                SupportHelpRevision.language == "ru",
                SupportHelpRevision.status == "published",
                SupportHelpRevision.published_at <= now,
                SupportHelpRevision.review_until > now,
            )
            .distinct()
        )
    ).all()
    best: tuple[tuple[int, ...], str] | None = None
    for version in versions:
        key = _version_key(version)
        if key is not None and key <= wanted and (best is None or key > best[0]):
            best = (key, version)
    return best[1] if best else requested
