"""Read-only article suggestions; never a generated answer or a resolved ticket."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tourism_backend.api.errors import AppError
from tourism_backend.modules.support.infrastructure.help_models import SupportHelpRevision


class HelpArticleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: str
    revision: int
    app_version: str
    title: str
    question: str
    excerpt: str
    body: str | None = None


class HelpSearchOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[HelpArticleOut] = Field(default_factory=list)
    method: str = "full_text"
    # Separate a miss from a not-yet-published corpus for this exact build.
    available: bool


class HelpSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=400)
    app_version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=32)


def _visible(app_version: str) -> tuple[ColumnElement[bool], ...]:
    now = datetime.now(UTC)
    return (
        SupportHelpRevision.app_version == app_version,
        SupportHelpRevision.language == "ru",
        SupportHelpRevision.status == "published",
        SupportHelpRevision.published_at <= now,
        SupportHelpRevision.review_until > now,
    )


def _out(row: SupportHelpRevision, *, detail: bool = False) -> HelpArticleOut:
    return HelpArticleOut(
        article_id=row.article_id,
        revision=row.revision,
        app_version=row.app_version,
        title=row.title,
        question=row.question,
        excerpt=row.body.split("\n\n", 1)[0][:700],
        body=row.body if detail else None,
    )


async def search_help(session: AsyncSession, *, query: str, app_version: str) -> HelpSearchOut:
    available = await session.scalar(
        select(SupportHelpRevision.id).where(*_visible(app_version)).limit(1)
    )
    if available is None or not query.strip():
        return HelpSearchOut(available=available is not None)
    # A bounded literal query, not tsquery syntax supplied by the client.
    # Russian FTS handles word forms; no LLM, embedder or external calls.
    ids = await _search_ids(session, query=query, app_version=app_version, relaxed=False)
    if not ids:
        # An inflected verb may not share the noun's stem ("начислились" vs
        # "начисление"). Relax only the heading/question match, never search
        # unrelated body boilerplate with a broad OR or bypass visibility.
        ids = await _search_ids(session, query=query, app_version=app_version, relaxed=True)
    rows = list(
        (
            await session.scalars(
                select(SupportHelpRevision).where(
                    SupportHelpRevision.id.in_(ids), *_visible(app_version)
                )
            )
        ).all()
    )
    by_id = {row.id: row for row in rows}
    return HelpSearchOut(available=True, items=[_out(by_id[id_]) for id_ in ids if id_ in by_id])


async def _search_ids(
    session: AsyncSession,
    *,
    query: str,
    app_version: str,
    relaxed: bool,
) -> list[UUID]:
    query_expression = (
        "to_tsquery('russian', (SELECT string_agg(quote_literal(term), ' | ') "
        "FROM unnest(tsvector_to_array(to_tsvector('russian', :q))) AS term))"
        if relaxed
        else "plainto_tsquery('russian', :q)"
    )
    match_expression = (
        "to_tsvector('russian', title || ' ' || question)"
        if relaxed
        else "to_tsvector('russian', title || ' ' || question || ' ' || body)"
    )
    # Both SQL fragments are chosen from fixed literals, never client text.
    statement = text(
        "".join(
            (
                "SELECT id FROM support_help_revisions, ",
                query_expression,
                " AS query ",
                "WHERE app_version = :version AND language = 'ru' AND status = 'published' ",
                "AND published_at <= :now AND review_until > :now AND ",
                match_expression,
                " @@ query ",
                "ORDER BY ts_rank_cd("
                "setweight(to_tsvector('russian', title || ' ' || question), 'A') || "
                "setweight(to_tsvector('russian', body), 'B'), query) DESC, article_id LIMIT 3",
            )
        )
    )
    return list(
        (
            await session.scalars(
                statement,
                {
                    "q": query[:400],
                    "version": app_version,
                    "now": datetime.now(UTC),
                },
            )
        ).all()
    )


async def read_help(
    session: AsyncSession,
    *,
    article_id: str,
    revision: int,
    app_version: str,
) -> HelpArticleOut:
    row = await session.scalar(
        select(SupportHelpRevision).where(
            SupportHelpRevision.article_id == article_id,
            SupportHelpRevision.revision == revision,
            *_visible(app_version),
        )
    )
    if row is None:
        raise AppError(
            code="help_article_unavailable", message="Статья недоступна", status_code=404
        )
    return _out(row, detail=True)
