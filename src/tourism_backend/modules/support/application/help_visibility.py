"""One publication gate for lexical search, indexing, semantic search and reading."""

from datetime import UTC, datetime

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
