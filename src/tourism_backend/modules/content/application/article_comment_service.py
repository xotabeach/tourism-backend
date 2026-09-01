"""Article comments (Workstream G, step 4).

Its own file rather than more weight in ``article_service.py``: a comment
is a separate entity with its own lifecycle, and the project already
splits ``review_service.py`` out of ``service.py`` for the same reason.

Deliberately not a review: no rating, because this discusses the text
rather than scoring a route.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.content.application.article_schemas import (
    ArticleCommentCreateIn,
    ArticleCommentListOut,
    ArticleCommentOut,
)
from tourism_backend.modules.content.infrastructure.models import Article, ArticleComment
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.media.application import service as media_service

# Same window reviews use: long enough to undo a regretted comment, short
# enough that a conversation others have replied to cannot be rewritten.
_COMMENT_DELETE_WINDOW = timedelta(hours=6)
_ANONYMOUS_AUTHOR = "Путешественник"


def _comment_not_found() -> AppError:
    return AppError(
        code="article_comment_not_found", message="Комментарий не найден", status_code=404
    )


async def _readable_article(
    session: AsyncSession,
    *,
    article_id: UUID,
    viewer_user_id: UUID | None,
) -> Article:
    """An article nobody may read has no readable comments either."""
    article = await session.get(Article, article_id)
    if article is None or article.status == "deleted":
        raise AppError(code="article_not_found", message="Статья не найдена", status_code=404)
    if article.status != "published" and article.author_user_id != viewer_user_id:
        raise AppError(code="article_not_found", message="Статья не найдена", status_code=404)
    return article


def _comment_out(
    comment: ArticleComment,
    *,
    authors: dict[UUID, User],
    avatars: dict[UUID, str],
) -> ArticleCommentOut:
    author = authors.get(comment.author_user_id)
    return ArticleCommentOut(
        id=str(comment.id),
        article_id=str(comment.article_id),
        author_user_id=str(comment.author_user_id),
        author_display_name=author.display_name if author is not None else _ANONYMOUS_AUTHOR,
        author_avatar_url=avatars.get(comment.author_user_id),
        body=comment.body,
        status=comment.status,  # type: ignore[arg-type]
        reply_to_comment_id=(
            str(comment.reply_to_comment_id) if comment.reply_to_comment_id else None
        ),
        created_at=comment.created_at,
    )


async def list_article_comments(
    session: AsyncSession,
    *,
    article_id: UUID,
    viewer_user_id: UUID | None,
    limit: int = 50,
    offset: int = 0,
) -> ArticleCommentListOut:
    """Published comments, plus the viewer's own still awaiting moderation.

    Without that second half a commenter would post and see nothing, and
    reasonably conclude the feature is broken.
    """
    await _readable_article(session, article_id=article_id, viewer_user_id=viewer_user_id)

    visible = ArticleComment.status == "published"
    if viewer_user_id is not None:
        own_pending = (ArticleComment.author_user_id == viewer_user_id) & (
            ArticleComment.status == "pending_review"
        )
        visible = visible | own_pending

    filters = (ArticleComment.article_id == article_id, visible)
    total = int(
        await session.scalar(select(func.count()).select_from(ArticleComment).where(*filters)) or 0
    )
    rows = list(
        (
            await session.scalars(
                select(ArticleComment)
                .where(*filters)
                .order_by(ArticleComment.created_at, ArticleComment.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    author_ids = list({row.author_user_id for row in rows})
    authors = (
        {
            user.id: user
            for user in (await session.scalars(select(User).where(User.id.in_(author_ids)))).all()
        }
        if author_ids
        else {}
    )
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=author_ids,
        role="avatar",
    )
    return ArticleCommentListOut(
        items=[_comment_out(row, authors=authors, avatars=avatars) for row in rows],
        total=total,
    )


async def create_article_comment(
    session: AsyncSession,
    *,
    article_id: UUID,
    author_user_id: UUID,
    payload: ArticleCommentCreateIn,
) -> ArticleCommentOut:
    article = await session.get(Article, article_id)
    if article is None or article.status != "published":
        # Only a published article is a place to hold a conversation; a
        # draft's existence stays unprovable to strangers either way.
        raise AppError(code="article_not_found", message="Статья не найдена", status_code=404)

    if payload.reply_to_comment_id is not None:
        parent = await session.get(ArticleComment, payload.reply_to_comment_id)
        if parent is None or parent.article_id != article.id or parent.status != "published":
            raise AppError(
                code="article_comment_parent_not_found",
                message="Комментарий, на который вы отвечаете, не найден",
                status_code=400,
            )

    now = datetime.now(UTC)
    comment = ArticleComment(
        id=uuid4(),
        article_id=article.id,
        author_user_id=author_user_id,
        reply_to_comment_id=payload.reply_to_comment_id,
        body=payload.body,
        status="pending_review",
        created_at=now,
        updated_at=now,
    )
    session.add(comment)
    await session.commit()

    author = await session.get(User, author_user_id)
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[author_user_id],
        role="avatar",
    )
    return _comment_out(
        comment,
        authors={author.id: author} if author is not None else {},
        avatars=avatars,
    )


async def delete_own_comment(
    session: AsyncSession,
    *,
    article_id: UUID,
    comment_id: UUID,
    author_user_id: UUID,
) -> None:
    """Soft-delete own comment within 6 hours of posting."""
    comment = await session.get(ArticleComment, comment_id)
    if comment is None or comment.article_id != article_id or comment.status == "deleted":
        raise _comment_not_found()
    if comment.author_user_id != author_user_id:
        raise _comment_not_found()

    created = comment.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if datetime.now(UTC) - created > _COMMENT_DELETE_WINDOW:
        raise AppError(
            code="article_comment_delete_window_expired",
            message="Удалить комментарий можно только в течение 6 часов после публикации",
            status_code=409,
        )

    comment.status = "deleted"
    comment.updated_at = datetime.now(UTC)
    await session.commit()


async def set_comment_status(
    session: AsyncSession,
    *,
    comment_ids: list[UUID],
    status: str,
) -> int:
    """Moderation entry point, mirroring set_review_status."""
    if status not in {"published", "rejected", "deleted"}:
        raise AppError(code="validation_error", message="Invalid status", status_code=400)
    if not comment_ids:
        return 0
    rows = list(
        (
            await session.scalars(select(ArticleComment).where(ArticleComment.id.in_(comment_ids)))
        ).all()
    )
    now = datetime.now(UTC)
    changed = 0
    for comment in rows:
        if status in {"published", "rejected"} and comment.status != "pending_review":
            continue
        comment.status = status
        comment.moderated_at = now
        comment.updated_at = now
        changed += 1
    await session.commit()
    return changed
