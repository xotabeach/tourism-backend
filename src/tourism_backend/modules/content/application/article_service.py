"""Article read/write/moderation (Workstream G, step 2).

Follows ``routes/application/review_service.py`` closely on purpose:
same signature style (``session`` positional, everything else
keyword-only), same batched author/avatar resolution instead of
per-card lookups, same "archive the attachment row, then unlink the
file" ordering so the database stays authoritative if the unlink fails.
"""

from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import get_settings
from tourism_backend.modules.content.application.article_media import (
    SavedArticleImage,
    delete_article_image,
)
from tourism_backend.modules.content.application.article_schemas import (
    ArticleBlockOut,
    ArticleLikeStatusOut,
    ArticleListOut,
    ArticleOut,
    ArticleSaveStatusOut,
    ArticleSummaryOut,
    ArticleWriteIn,
)
from tourism_backend.modules.content.infrastructure.models import (
    MAX_IMAGES_PER_ARTICLE,
    Article,
    ArticleBlock,
    ArticleBookmark,
    ArticleLike,
)
from tourism_backend.modules.identity.infrastructure.models import (
    EXPERT_RANK_ID,
    TravelRank,
    User,
)
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.notifications.application import service as notifications_service
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route

# An article is edited as a whole, so "editable" is a status question, not
# a time window like a review's photo edits.
_EDITABLE_STATUSES = frozenset({"draft", "rejected"})
_SUBMIT_WINDOW = timedelta(hours=24)
_MAX_SUBMISSIONS_PER_WINDOW = 3
_ANONYMOUS_AUTHOR = "Путешественник"
_EXCERPT_LENGTH = 200
# Rough Russian silent-reading speed, in characters per minute — good enough
# for a "N мин чтения" estimate, not meant to be precise.
_READING_CHARS_PER_MINUTE = 800
_RELATED_ARTICLES_LIMIT = 4


def _not_found() -> AppError:
    return AppError(code="article_not_found", message="Статья не найдена", status_code=404)


async def _authors(session: AsyncSession, user_ids: list[UUID]) -> dict[UUID, User]:
    if not user_ids:
        return {}
    rows = (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
    return {user.id: user for user in rows}


async def _rank_titles(session: AsyncSession, users: list[User]) -> dict[UUID, str]:
    """Travel rank title per user, by ``travel_points``.

    Same resolution as ``routes/application/service.py`` uses for a route's
    owner, kept here rather than shared so the content module does not reach
    into another module's application layer for it.
    """
    if not users:
        return {}
    ranks_sorted = sorted(
        (await session.scalars(select(TravelRank).where(TravelRank.id != EXPERT_RANK_ID))).all(),
        key=lambda rank: rank.min_points,
        reverse=True,
    )
    out: dict[UUID, str] = {}
    for user in users:
        if getattr(user, "is_expert", False):
            out[user.id] = "Эксперт"
            continue
        title = "Новичок"
        for rank in ranks_sorted:
            if user.travel_points >= rank.min_points:
                title = rank.title
                break
        out[user.id] = title
    return out


async def _cover_urls(session: AsyncSession, articles: list[Article]) -> dict[UUID, str]:
    """Resolve cover images in one query keyed by article id."""
    wanted = {
        article.cover_media_attachment_id: article.id
        for article in articles
        if article.cover_media_attachment_id is not None
    }
    if not wanted:
        return {}
    rows = (
        await session.scalars(
            select(MediaAttachment).where(
                MediaAttachment.id.in_(list(wanted)),
                MediaAttachment.status == "active",
            )
        )
    ).all()
    return {wanted[row.id]: row.public_path for row in rows if row.id in wanted}


def _truncate_excerpt(text: str, *, limit: int = _EXCERPT_LENGTH) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    boundary = cut.rfind(" ")
    if boundary > 0:
        cut = cut[:boundary]
    return f"{cut.rstrip()}…"


async def _excerpts_and_reading_times(
    session: AsyncSession, articles: list[Article]
) -> tuple[dict[UUID, str | None], dict[UUID, int]]:
    """Batch-computed, not stored — same principle as route rating aggregates.

    Excerpt comes only from the first genuine ``text`` block (a quote or a
    list item makes a strange opening line for a card preview); reading time
    sums every block that carries prose (``text``/``quote``/``list``).
    """
    if not articles:
        return {}, {}
    article_ids = [article.id for article in articles]
    rows = list(
        (
            await session.scalars(
                select(ArticleBlock)
                .where(
                    ArticleBlock.article_id.in_(article_ids),
                    ArticleBlock.block_type.in_(("text", "quote", "list")),
                )
                .order_by(ArticleBlock.article_id, ArticleBlock.position)
            )
        ).all()
    )
    by_article: dict[UUID, list[ArticleBlock]] = {}
    for block in rows:
        by_article.setdefault(block.article_id, []).append(block)

    excerpts: dict[UUID, str | None] = {}
    reading_times: dict[UUID, int] = {}
    for article_id, blocks in by_article.items():
        first_text_content = next(
            (
                block.text_content
                for block in blocks
                if block.block_type == "text" and block.text_content
            ),
            None,
        )
        excerpts[article_id] = (
            _truncate_excerpt(first_text_content) if first_text_content is not None else None
        )
        total_chars = sum(len(block.text_content or "") for block in blocks)
        reading_times[article_id] = max(1, ceil(total_chars / _READING_CHARS_PER_MINUTE))
    for article in articles:
        excerpts.setdefault(article.id, None)
        reading_times.setdefault(article.id, 1)
    return excerpts, reading_times


async def _liked_article_ids(
    session: AsyncSession, viewer_user_id: UUID | None, article_ids: list[UUID]
) -> set[UUID]:
    if viewer_user_id is None or not article_ids:
        return set()
    rows = await session.scalars(
        select(ArticleLike.article_id).where(
            ArticleLike.user_id == viewer_user_id,
            ArticleLike.article_id.in_(article_ids),
        )
    )
    return set(rows.all())


async def _saved_article_ids(
    session: AsyncSession, viewer_user_id: UUID | None, article_ids: list[UUID]
) -> set[UUID]:
    if viewer_user_id is None or not article_ids:
        return set()
    rows = await session.scalars(
        select(ArticleBookmark.article_id).where(
            ArticleBookmark.user_id == viewer_user_id,
            ArticleBookmark.article_id.in_(article_ids),
        )
    )
    return set(rows.all())


def _summary_out(
    article: Article,
    *,
    authors: dict[UUID, User],
    avatars: dict[UUID, str],
    ranks: dict[UUID, str],
    covers: dict[UUID, str],
    excerpts: dict[UUID, str | None],
    reading_times: dict[UUID, int],
    liked_article_ids: set[UUID],
    saved_article_ids: set[UUID],
) -> ArticleSummaryOut:
    author = authors.get(article.author_user_id)
    return ArticleSummaryOut(
        id=str(article.id),
        title=article.title,
        status=article.status,  # type: ignore[arg-type]
        author_user_id=str(article.author_user_id),
        author_display_name=author.display_name if author is not None else _ANONYMOUS_AUTHOR,
        author_avatar_url=avatars.get(article.author_user_id),
        author_rank_title=ranks.get(article.author_user_id),
        related_route_id=str(article.related_route_id) if article.related_route_id else None,
        related_place_id=str(article.related_place_id) if article.related_place_id else None,
        cover_image_url=covers.get(article.id),
        tags=list(article.tags),
        excerpt=excerpts.get(article.id),
        reading_time_minutes=reading_times.get(article.id, 1),
        like_count=article.like_count,
        liked_by_me=article.id in liked_article_ids,
        saved_by_me=article.id in saved_article_ids,
        view_count=article.view_count,
        is_featured=article.is_featured,
        created_at=article.created_at,
        published_at=article.published_at,
    )


async def _blocks_out(session: AsyncSession, article_id: UUID) -> list[ArticleBlockOut]:
    blocks = list(
        (
            await session.scalars(
                select(ArticleBlock)
                .where(ArticleBlock.article_id == article_id)
                .order_by(ArticleBlock.position)
            )
        ).all()
    )
    attachment_ids = [
        block.media_attachment_id for block in blocks if block.media_attachment_id is not None
    ]
    attachments: dict[UUID, MediaAttachment] = {}
    if attachment_ids:
        rows = (
            await session.scalars(
                select(MediaAttachment).where(
                    MediaAttachment.id.in_(attachment_ids),
                    MediaAttachment.status == "active",
                )
            )
        ).all()
        attachments = {row.id: row for row in rows}
    result: list[ArticleBlockOut] = []
    for block in blocks:
        attachment = (
            attachments.get(block.media_attachment_id)
            if block.media_attachment_id is not None
            else None
        )
        result.append(
            ArticleBlockOut(
                id=str(block.id),
                position=block.position,
                block_type=block.block_type,  # type: ignore[arg-type]
                text_content=block.text_content,
                image_url=attachment.public_path if attachment is not None else None,
                image_width=attachment.width if attachment is not None else None,
                image_height=attachment.height if attachment is not None else None,
            )
        )
    return result


async def _article_out(
    session: AsyncSession, article: Article, *, viewer_user_id: UUID | None
) -> ArticleOut:
    authors = await _authors(session, [article.author_user_id])
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[article.author_user_id],
        role="avatar",
    )
    ranks = await _rank_titles(session, list(authors.values()))
    covers = await _cover_urls(session, [article])
    excerpts, reading_times = await _excerpts_and_reading_times(session, [article])
    liked_ids = await _liked_article_ids(session, viewer_user_id, [article.id])
    saved_ids = await _saved_article_ids(session, viewer_user_id, [article.id])
    author = authors.get(article.author_user_id)
    return ArticleOut(
        id=str(article.id),
        title=article.title,
        status=article.status,  # type: ignore[arg-type]
        author_user_id=str(article.author_user_id),
        author_display_name=author.display_name if author is not None else _ANONYMOUS_AUTHOR,
        author_avatar_url=avatars.get(article.author_user_id),
        author_rank_title=ranks.get(article.author_user_id),
        related_route_id=str(article.related_route_id) if article.related_route_id else None,
        related_place_id=str(article.related_place_id) if article.related_place_id else None,
        cover_image_url=covers.get(article.id),
        moderator_note=article.moderator_note,
        tags=list(article.tags),
        excerpt=excerpts.get(article.id),
        reading_time_minutes=reading_times.get(article.id, 1),
        like_count=article.like_count,
        liked_by_me=article.id in liked_ids,
        saved_by_me=article.id in saved_ids,
        view_count=article.view_count,
        is_featured=article.is_featured,
        created_at=article.created_at,
        published_at=article.published_at,
        blocks=await _blocks_out(session, article.id),
    )


async def _validate_subject(session: AsyncSession, payload: ArticleWriteIn) -> None:
    """A dangling subject would render as a card pointing nowhere."""
    if payload.related_route_id is not None:
        route = await session.get(Route, payload.related_route_id)
        if route is None:
            raise AppError(
                code="article_subject_not_found",
                message="Маршрут не найден",
                status_code=400,
            )
    if payload.related_place_id is not None:
        place = await session.get(Place, payload.related_place_id)
        if place is None:
            raise AppError(
                code="article_subject_not_found",
                message="Локация не найдена",
                status_code=400,
            )


async def _replace_blocks(
    session: AsyncSession,
    *,
    article: Article,
    payload: ArticleWriteIn,
) -> None:
    """Rebuild the block list wholesale — blocks are not edited singly.

    Any image already uploaded into a block is dropped with it, so the
    files go too: keeping them would orphan bytes on disk that nothing
    references any more.
    """
    existing = list(
        (
            await session.scalars(select(ArticleBlock).where(ArticleBlock.article_id == article.id))
        ).all()
    )
    stale_attachment_ids = [
        block.media_attachment_id for block in existing if block.media_attachment_id is not None
    ]
    for block in existing:
        await session.delete(block)
    # Flush the deletes before inserting, or the (article_id, position)
    # unique constraint trips against rows that are on their way out.
    await session.flush()

    if stale_attachment_ids:
        attachments = (
            await session.scalars(
                select(MediaAttachment).where(MediaAttachment.id.in_(stale_attachment_ids))
            )
        ).all()
        now = datetime.now(UTC)
        for attachment in attachments:
            attachment.status = "archived"
            attachment.updated_at = now
            delete_article_image(attachment.storage_key, article_id=article.id)

    for position, block_in in enumerate(payload.blocks):
        session.add(
            ArticleBlock(
                id=uuid4(),
                article_id=article.id,
                position=position,
                block_type=block_in.block_type,
                text_content=block_in.text_content,
                caption=block_in.caption,
                list_style=block_in.list_style,
                media_attachment_id=None,
            )
        )
    article.cover_media_attachment_id = None


async def list_published_articles(
    session: AsyncSession,
    *,
    related_route_id: UUID | None = None,
    related_place_id: UUID | None = None,
    author_user_id: UUID | None = None,
    viewer_user_id: UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ArticleListOut:
    filters = [Article.status == "published"]
    if related_route_id is not None:
        filters.append(Article.related_route_id == related_route_id)
    if related_place_id is not None:
        filters.append(Article.related_place_id == related_place_id)
    if author_user_id is not None:
        filters.append(Article.author_user_id == author_user_id)

    total = int(
        await session.scalar(select(func.count()).select_from(Article).where(*filters)) or 0
    )
    rows = list(
        (
            await session.scalars(
                select(Article)
                .where(*filters)
                .order_by(Article.published_at.desc(), Article.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    author_ids = list({row.author_user_id for row in rows})
    authors = await _authors(session, author_ids)
    ranks = await _rank_titles(session, list(authors.values()))
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=author_ids,
        role="avatar",
    )
    covers = await _cover_urls(session, rows)
    excerpts, reading_times = await _excerpts_and_reading_times(session, rows)
    liked_ids = await _liked_article_ids(session, viewer_user_id, [row.id for row in rows])
    saved_ids = await _saved_article_ids(session, viewer_user_id, [row.id for row in rows])
    return ArticleListOut(
        items=[
            _summary_out(
                row,
                authors=authors,
                avatars=avatars,
                ranks=ranks,
                covers=covers,
                excerpts=excerpts,
                reading_times=reading_times,
                liked_article_ids=liked_ids,
                saved_article_ids=saved_ids,
            )
            for row in rows
        ],
        total=total,
    )


async def list_my_articles(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    limit: int = 20,
    offset: int = 0,
) -> ArticleListOut:
    """Every status except deleted — this is the author's own workspace."""
    filters = [Article.author_user_id == author_user_id, Article.status != "deleted"]
    total = int(
        await session.scalar(select(func.count()).select_from(Article).where(*filters)) or 0
    )
    rows = list(
        (
            await session.scalars(
                select(Article)
                .where(*filters)
                .order_by(Article.created_at.desc(), Article.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    authors = await _authors(session, [author_user_id])
    ranks = await _rank_titles(session, list(authors.values()))
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[author_user_id],
        role="avatar",
    )
    covers = await _cover_urls(session, rows)
    excerpts, reading_times = await _excerpts_and_reading_times(session, rows)
    liked_ids = await _liked_article_ids(session, author_user_id, [row.id for row in rows])
    saved_ids = await _saved_article_ids(session, author_user_id, [row.id for row in rows])
    return ArticleListOut(
        items=[
            _summary_out(
                row,
                authors=authors,
                avatars=avatars,
                ranks=ranks,
                covers=covers,
                excerpts=excerpts,
                reading_times=reading_times,
                liked_article_ids=liked_ids,
                saved_article_ids=saved_ids,
            )
            for row in rows
        ],
        total=total,
    )


async def get_article(
    session: AsyncSession,
    *,
    article_id: UUID,
    viewer_user_id: UUID | None,
) -> ArticleOut:
    article = await session.get(Article, article_id)
    if article is None or article.status == "deleted":
        raise _not_found()
    if article.status != "published" and article.author_user_id != viewer_user_id:
        # Same shape as a missing article on purpose: a draft's existence
        # is not something a stranger should be able to probe for.
        raise _not_found()
    if article.status == "published":
        # Atomic increment — avoids a read-then-write race under concurrent
        # views, and no dedup by viewer: an approximate counter is the point.
        await session.execute(
            update(Article)
            .where(Article.id == article.id)
            .values(view_count=Article.view_count + 1)
        )
        await session.commit()
        await session.refresh(article)
    return await _article_out(session, article, viewer_user_id=viewer_user_id)


async def create_article_draft(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    payload: ArticleWriteIn,
) -> ArticleOut:
    await _validate_subject(session, payload)
    now = datetime.now(UTC)
    article = Article(
        id=uuid4(),
        author_user_id=author_user_id,
        title=payload.title,
        status="draft",
        related_route_id=payload.related_route_id,
        related_place_id=payload.related_place_id,
        tags=payload.tags,
        created_at=now,
        updated_at=now,
    )
    session.add(article)
    await session.flush()
    for position, block_in in enumerate(payload.blocks):
        session.add(
            ArticleBlock(
                id=uuid4(),
                article_id=article.id,
                position=position,
                block_type=block_in.block_type,
                text_content=block_in.text_content,
                caption=block_in.caption,
                list_style=block_in.list_style,
                media_attachment_id=None,
            )
        )
    await session.commit()
    await session.refresh(article)
    return await _article_out(session, article, viewer_user_id=author_user_id)


async def _own_editable_article(
    session: AsyncSession,
    *,
    article_id: UUID,
    author_user_id: UUID,
) -> Article:
    article = await session.get(Article, article_id)
    if article is None or article.status == "deleted":
        raise _not_found()
    if article.author_user_id != author_user_id:
        raise _not_found()
    if article.status not in _EDITABLE_STATUSES:
        raise AppError(
            code="article_not_editable",
            message="Статью на модерации или уже опубликованную нельзя редактировать",
            status_code=409,
        )
    return article


async def update_article_draft(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    article_id: UUID,
    payload: ArticleWriteIn,
) -> ArticleOut:
    article = await _own_editable_article(
        session, article_id=article_id, author_user_id=author_user_id
    )
    await _validate_subject(session, payload)
    article.title = payload.title
    article.related_route_id = payload.related_route_id
    article.related_place_id = payload.related_place_id
    article.tags = payload.tags
    article.updated_at = datetime.now(UTC)
    await _replace_blocks(session, article=article, payload=payload)
    await session.commit()
    await session.refresh(article)
    return await _article_out(session, article, viewer_user_id=author_user_id)


async def submit_article_for_review(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    article_id: UUID,
) -> ArticleOut:
    article = await _own_editable_article(
        session, article_id=article_id, author_user_id=author_user_id
    )
    blocks = int(
        await session.scalar(
            select(func.count())
            .select_from(ArticleBlock)
            .where(ArticleBlock.article_id == article.id)
        )
        or 0
    )
    if blocks == 0:
        raise AppError(
            code="article_empty",
            message="Нельзя отправить на модерацию пустую статью",
            status_code=400,
        )
    since = datetime.now(UTC) - _SUBMIT_WINDOW
    recent = int(
        await session.scalar(
            select(func.count())
            .select_from(Article)
            .where(
                Article.author_user_id == author_user_id,
                Article.status.in_(("pending_review", "published")),
                Article.updated_at >= since,
            )
        )
        or 0
    )
    if recent >= _MAX_SUBMISSIONS_PER_WINDOW:
        raise AppError(
            code="article_quota_exceeded",
            message="Достигнут дневной лимит отправки статей на модерацию",
            status_code=429,
        )
    article.status = "pending_review"
    article.moderator_note = None
    article.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(article)
    return await _article_out(session, article, viewer_user_id=author_user_id)


async def delete_own_article(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    article_id: UUID,
) -> None:
    article = await session.get(Article, article_id)
    if article is None or article.status == "deleted":
        raise _not_found()
    if article.author_user_id != author_user_id:
        raise _not_found()
    article.status = "deleted"
    article.updated_at = datetime.now(UTC)
    await session.commit()


async def set_article_like(
    session: AsyncSession,
    *,
    article_id: UUID,
    user_id: UUID,
    liked: bool,
) -> ArticleLikeStatusOut:
    """Idempotent toggle, same shape as `favorites/application/service.py`'s
    add/remove pair — only a published article can be liked."""
    article = await session.get(Article, article_id)
    if article is None or article.status != "published":
        raise _not_found()
    existing = await session.get(ArticleLike, (article_id, user_id))
    if liked and existing is None:
        session.add(
            ArticleLike(article_id=article_id, user_id=user_id, created_at=datetime.now(UTC))
        )
        article.like_count += 1
        await session.commit()
    elif not liked and existing is not None:
        await session.delete(existing)
        article.like_count = max(0, article.like_count - 1)
        await session.commit()
    await session.refresh(article)
    return ArticleLikeStatusOut(like_count=article.like_count, liked_by_me=liked)


async def set_article_saved(
    session: AsyncSession,
    *,
    article_id: UUID,
    user_id: UUID,
    saved: bool,
) -> ArticleSaveStatusOut:
    """A bookmark is private and unlike a like carries no counter — nothing
    about the article itself changes, only this user's reading list."""
    article = await session.get(Article, article_id)
    if article is None or article.status != "published":
        raise _not_found()
    existing = await session.get(ArticleBookmark, (article_id, user_id))
    if saved and existing is None:
        session.add(
            ArticleBookmark(article_id=article_id, user_id=user_id, created_at=datetime.now(UTC))
        )
        await session.commit()
    elif not saved and existing is not None:
        await session.delete(existing)
        await session.commit()
    return ArticleSaveStatusOut(saved_by_me=saved)


async def list_saved_articles(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int = 20,
    offset: int = 0,
) -> ArticleListOut:
    """The favorites screen's "Статьи" section — newest bookmark first, and
    an article that has since been unpublished simply drops out."""
    filters = (ArticleBookmark.user_id == user_id, Article.status == "published")
    base = select(Article).join(ArticleBookmark, ArticleBookmark.article_id == Article.id)
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(Article)
            .join(ArticleBookmark, ArticleBookmark.article_id == Article.id)
            .where(*filters)
        )
        or 0
    )
    rows = list(
        (
            await session.scalars(
                base.where(*filters)
                .order_by(ArticleBookmark.created_at.desc(), Article.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    author_ids = list({row.author_user_id for row in rows})
    authors = await _authors(session, author_ids)
    ranks = await _rank_titles(session, list(authors.values()))
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=author_ids,
        role="avatar",
    )
    covers = await _cover_urls(session, rows)
    excerpts, reading_times = await _excerpts_and_reading_times(session, rows)
    liked_ids = await _liked_article_ids(session, user_id, [row.id for row in rows])
    saved_ids = await _saved_article_ids(session, user_id, [row.id for row in rows])
    return ArticleListOut(
        items=[
            _summary_out(
                row,
                authors=authors,
                avatars=avatars,
                ranks=ranks,
                covers=covers,
                excerpts=excerpts,
                reading_times=reading_times,
                liked_article_ids=liked_ids,
                saved_article_ids=saved_ids,
            )
            for row in rows
        ],
        total=total,
    )


async def list_related_articles(
    session: AsyncSession,
    *,
    article_id: UUID,
    viewer_user_id: UUID | None,
    limit: int = _RELATED_ARTICLES_LIMIT,
) -> ArticleListOut:
    """Published articles sharing at least one tag — plain SQL, not a
    separate ML pass, same "aggregate at query time" principle used
    elsewhere in this module."""
    article = await session.get(Article, article_id)
    if article is None or article.status == "deleted":
        raise _not_found()
    if not article.tags:
        return ArticleListOut(items=[], total=0)

    filters = (
        Article.status == "published",
        Article.id != article.id,
        Article.tags.overlap(article.tags),
    )
    total = int(
        await session.scalar(select(func.count()).select_from(Article).where(*filters)) or 0
    )
    rows = list(
        (
            await session.scalars(
                select(Article)
                .where(*filters)
                .order_by(Article.published_at.desc(), Article.id)
                .limit(limit)
            )
        ).all()
    )
    author_ids = list({row.author_user_id for row in rows})
    authors = await _authors(session, author_ids)
    ranks = await _rank_titles(session, list(authors.values()))
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=author_ids,
        role="avatar",
    )
    covers = await _cover_urls(session, rows)
    excerpts, reading_times = await _excerpts_and_reading_times(session, rows)
    liked_ids = await _liked_article_ids(session, viewer_user_id, [row.id for row in rows])
    saved_ids = await _saved_article_ids(session, viewer_user_id, [row.id for row in rows])
    return ArticleListOut(
        items=[
            _summary_out(
                row,
                authors=authors,
                avatars=avatars,
                ranks=ranks,
                covers=covers,
                excerpts=excerpts,
                reading_times=reading_times,
                liked_article_ids=liked_ids,
                saved_article_ids=saved_ids,
            )
            for row in rows
        ],
        total=total,
    )


async def ensure_own_article_block(
    session: AsyncSession,
    *,
    article_id: UUID,
    block_id: UUID,
    author_user_id: UUID,
) -> tuple[Article, ArticleBlock]:
    article = await _own_editable_article(
        session, article_id=article_id, author_user_id=author_user_id
    )
    block = await session.get(ArticleBlock, block_id)
    if block is None or block.article_id != article.id:
        raise AppError(code="article_block_not_found", message="Блок не найден", status_code=404)
    if block.block_type != "image":
        raise AppError(
            code="article_block_not_image",
            message="Картинку можно приложить только к image-блоку",
            status_code=409,
        )
    return article, block


async def add_article_block_image(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    article_id: UUID,
    block_id: UUID,
    saved: SavedArticleImage,
) -> ArticleBlockOut:
    article, block = await ensure_own_article_block(
        session, article_id=article_id, block_id=block_id, author_user_id=author_user_id
    )
    active = (
        MediaAttachment.entity_type == "article",
        MediaAttachment.entity_id == article.id,
        MediaAttachment.role == "gallery",
        MediaAttachment.status == "active",
    )
    if block.media_attachment_id is not None:
        # Replacing a block's image: the old one stops being referenced the
        # moment the new id lands, so archive and unlink it here.
        previous = await session.get(MediaAttachment, block.media_attachment_id)
        if previous is not None:
            previous.status = "archived"
            previous.updated_at = datetime.now(UTC)
            delete_article_image(previous.storage_key, article_id=article.id)
        block.media_attachment_id = None
        await session.flush()

    count = int(
        await session.scalar(select(func.count()).select_from(MediaAttachment).where(*active)) or 0
    )
    if count >= MAX_IMAGES_PER_ARTICLE:
        delete_article_image(saved.storage_key, article_id=article.id)
        raise AppError(
            code="article_media_limit",
            message=f"К статье можно приложить не больше {MAX_IMAGES_PER_ARTICLE} изображений",
            status_code=409,
        )

    now = datetime.now(UTC)
    attachment = MediaAttachment(
        id=uuid4(),
        entity_type="article",
        entity_id=article.id,
        role="gallery",
        storage_key=saved.storage_key,
        public_path=saved.public_path,
        content_type=saved.content_type,
        byte_size=saved.byte_size,
        width=saved.width,
        height=saved.height,
        checksum_sha256=saved.checksum_sha256,
        status="active",
        uploaded_by_user_id=author_user_id,
        sort_order=block.position,
        created_at=now,
        updated_at=now,
    )
    session.add(attachment)
    await session.flush()
    block.media_attachment_id = attachment.id
    # Flush before recomputing the cover: _refresh_cover asks the database
    # which blocks have an image, so the link has to be in the database and
    # not merely on the in-memory object for it to be counted.
    await session.flush()
    await _refresh_cover(session, article)
    article.updated_at = now
    await session.commit()
    return ArticleBlockOut(
        id=str(block.id),
        position=block.position,
        block_type="image",
        text_content=None,
        image_url=attachment.public_path,
        image_width=attachment.width,
        image_height=attachment.height,
    )


async def delete_article_block_image(
    session: AsyncSession,
    *,
    author_user_id: UUID,
    article_id: UUID,
    block_id: UUID,
) -> None:
    article, block = await ensure_own_article_block(
        session, article_id=article_id, block_id=block_id, author_user_id=author_user_id
    )
    if block.media_attachment_id is None:
        raise AppError(
            code="article_media_not_found", message="Изображение не найдено", status_code=404
        )
    attachment = await session.get(MediaAttachment, block.media_attachment_id)
    block.media_attachment_id = None
    if attachment is not None:
        attachment.status = "archived"
        attachment.updated_at = datetime.now(UTC)
    await session.flush()
    await _refresh_cover(session, article)
    article.updated_at = datetime.now(UTC)
    await session.commit()
    if attachment is not None:
        delete_article_image(attachment.storage_key, article_id=article.id)


async def _refresh_cover(session: AsyncSession, article: Article) -> None:
    """Point the cover at the first image block that still has a file."""
    first = await session.scalar(
        select(ArticleBlock)
        .where(
            ArticleBlock.article_id == article.id,
            ArticleBlock.block_type == "image",
            ArticleBlock.media_attachment_id.is_not(None),
        )
        .order_by(ArticleBlock.position)
        .limit(1)
    )
    article.cover_media_attachment_id = first.media_attachment_id if first is not None else None


async def set_article_status(
    session: AsyncSession,
    *,
    article_ids: list[UUID],
    status: str,
) -> int:
    """Moderation entry point, mirroring set_review_status.

    Publishing stamps ``published_at`` separately from ``moderated_at``:
    the feed sorts by when an article went live, and ``moderated_at`` is
    also stamped on a rejection.
    """
    if status not in {"published", "rejected", "deleted"}:
        raise AppError(code="validation_error", message="Invalid status", status_code=400)
    if not article_ids:
        return 0
    rows = list((await session.scalars(select(Article).where(Article.id.in_(article_ids)))).all())
    settings = get_settings()
    now = datetime.now(UTC)
    changed = 0
    for article in rows:
        if status in {"published", "rejected"} and article.status != "pending_review":
            continue
        article.status = status
        article.moderated_at = now
        article.updated_at = now
        if status == "published":
            article.published_at = now
        changed += 1

        if status in {"published", "rejected"}:
            author_notif = await notifications_service.create_article_moderation_notification(
                session,
                author_user_id=article.author_user_id,
                article_id=article.id,
                article_title=article.title,
                approved=status == "published",
            )
            await notifications_service.maybe_push_notification(
                session,
                settings,
                user_id=article.author_user_id,
                kind=author_notif.kind,
                title=author_notif.title,
                body=author_notif.body,
                target_type="article",
                target_id=article.id,
            )

        if status == "published" and article.related_route_id is not None:
            route = await session.get(Route, article.related_route_id)
            if route is not None and route.owner_user_id is not None:
                owner_notif = await notifications_service.create_article_about_route_notification(
                    session,
                    owner_user_id=route.owner_user_id,
                    actor_user_id=article.author_user_id,
                    article_id=article.id,
                    article_title=article.title,
                )
                if owner_notif is not None:
                    await notifications_service.maybe_push_notification(
                        session,
                        settings,
                        user_id=route.owner_user_id,
                        kind=owner_notif.kind,
                        title=owner_notif.title,
                        body=owner_notif.body,
                        target_type="article",
                        target_id=article.id,
                    )
    await session.commit()
    return changed
