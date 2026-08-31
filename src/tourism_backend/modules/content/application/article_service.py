"""Article read/write/moderation (Workstream G, step 2).

Follows ``routes/application/review_service.py`` closely on purpose:
same signature style (``session`` positional, everything else
keyword-only), same batched author/avatar resolution instead of
per-card lookups, same "archive the attachment row, then unlink the
file" ordering so the database stays authoritative if the unlink fails.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.content.application.article_media import (
    SavedArticleImage,
    delete_article_image,
)
from tourism_backend.modules.content.application.article_schemas import (
    ArticleBlockOut,
    ArticleListOut,
    ArticleOut,
    ArticleSummaryOut,
    ArticleWriteIn,
)
from tourism_backend.modules.content.infrastructure.models import (
    MAX_IMAGES_PER_ARTICLE,
    Article,
    ArticleBlock,
)
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.media.application import service as media_service
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route

# An article is edited as a whole, so "editable" is a status question, not
# a time window like a review's photo edits.
_EDITABLE_STATUSES = frozenset({"draft", "rejected"})
_SUBMIT_WINDOW = timedelta(hours=24)
_MAX_SUBMISSIONS_PER_WINDOW = 3
_ANONYMOUS_AUTHOR = "Путешественник"


def _not_found() -> AppError:
    return AppError(code="article_not_found", message="Статья не найдена", status_code=404)


async def _authors(session: AsyncSession, user_ids: list[UUID]) -> dict[UUID, User]:
    if not user_ids:
        return {}
    rows = (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
    return {user.id: user for user in rows}


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


def _summary_out(
    article: Article,
    *,
    authors: dict[UUID, User],
    avatars: dict[UUID, str],
    covers: dict[UUID, str],
) -> ArticleSummaryOut:
    author = authors.get(article.author_user_id)
    return ArticleSummaryOut(
        id=str(article.id),
        title=article.title,
        status=article.status,  # type: ignore[arg-type]
        author_user_id=str(article.author_user_id),
        author_display_name=author.display_name if author is not None else _ANONYMOUS_AUTHOR,
        author_avatar_url=avatars.get(article.author_user_id),
        related_route_id=str(article.related_route_id) if article.related_route_id else None,
        related_place_id=str(article.related_place_id) if article.related_place_id else None,
        cover_image_url=covers.get(article.id),
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


async def _article_out(session: AsyncSession, article: Article) -> ArticleOut:
    authors = await _authors(session, [article.author_user_id])
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[article.author_user_id],
        role="avatar",
    )
    covers = await _cover_urls(session, [article])
    author = authors.get(article.author_user_id)
    return ArticleOut(
        id=str(article.id),
        title=article.title,
        status=article.status,  # type: ignore[arg-type]
        author_user_id=str(article.author_user_id),
        author_display_name=author.display_name if author is not None else _ANONYMOUS_AUTHOR,
        author_avatar_url=avatars.get(article.author_user_id),
        related_route_id=str(article.related_route_id) if article.related_route_id else None,
        related_place_id=str(article.related_place_id) if article.related_place_id else None,
        cover_image_url=covers.get(article.id),
        moderator_note=article.moderator_note,
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
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=author_ids,
        role="avatar",
    )
    covers = await _cover_urls(session, rows)
    return ArticleListOut(
        items=[_summary_out(row, authors=authors, avatars=avatars, covers=covers) for row in rows],
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
    avatars = await media_service.resolve_urls(
        session,
        entity_type="user",
        entity_ids=[author_user_id],
        role="avatar",
    )
    covers = await _cover_urls(session, rows)
    return ArticleListOut(
        items=[_summary_out(row, authors=authors, avatars=avatars, covers=covers) for row in rows],
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
    return await _article_out(session, article)


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
                media_attachment_id=None,
            )
        )
    await session.commit()
    await session.refresh(article)
    return await _article_out(session, article)


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
    article.updated_at = datetime.now(UTC)
    await _replace_blocks(session, article=article, payload=payload)
    await session.commit()
    await session.refresh(article)
    return await _article_out(session, article)


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
    return await _article_out(session, article)


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
