"""HTTP surface for articles (Workstream G, step 3).

One router rather than reviews' split across routes/ and places/: an
article's subject is a query filter, not a path segment, so there is no
reason to mount the same handlers twice.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Query, UploadFile, status

from tourism_backend.api.deps import CurrentUserId, DbSession, OptionalCurrentUserId
from tourism_backend.modules.content.application import (
    article_comment_service,
    article_media,
    article_service,
)
from tourism_backend.modules.content.application.article_schemas import (
    ArticleBlockOut,
    ArticleCommentCreateIn,
    ArticleCommentListOut,
    ArticleCommentOut,
    ArticleListOut,
    ArticleOut,
    ArticleWriteIn,
)

router = APIRouter(tags=["articles"])


@router.get("/articles", response_model=ArticleListOut)
async def list_articles(
    session: DbSession,
    related_route_id: Annotated[UUID | None, Query()] = None,
    related_place_id: Annotated[UUID | None, Query()] = None,
    author_user_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ArticleListOut:
    """Published articles only — readable without an account, like the catalog."""
    return await article_service.list_published_articles(
        session,
        related_route_id=related_route_id,
        related_place_id=related_place_id,
        author_user_id=author_user_id,
        limit=limit,
        offset=offset,
    )


@router.get("/me/articles", response_model=ArticleListOut)
async def list_my_articles(
    session: DbSession,
    user_id: CurrentUserId,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ArticleListOut:
    return await article_service.list_my_articles(
        session, author_user_id=user_id, limit=limit, offset=offset
    )


@router.get("/articles/{article_id}", response_model=ArticleOut)
async def get_article(
    article_id: UUID,
    session: DbSession,
    viewer_user_id: OptionalCurrentUserId,
) -> ArticleOut:
    """Published to everyone; anything else only to its author."""
    return await article_service.get_article(
        session, article_id=article_id, viewer_user_id=viewer_user_id
    )


@router.post("/articles", response_model=ArticleOut, status_code=status.HTTP_201_CREATED)
async def create_article(
    payload: ArticleWriteIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> ArticleOut:
    return await article_service.create_article_draft(
        session, author_user_id=user_id, payload=payload
    )


@router.patch("/articles/{article_id}", response_model=ArticleOut)
async def update_article(
    article_id: UUID,
    payload: ArticleWriteIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> ArticleOut:
    return await article_service.update_article_draft(
        session, author_user_id=user_id, article_id=article_id, payload=payload
    )


@router.post("/articles/{article_id}/submit", response_model=ArticleOut)
async def submit_article(
    article_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> ArticleOut:
    return await article_service.submit_article_for_review(
        session, author_user_id=user_id, article_id=article_id
    )


@router.delete("/articles/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_article(
    article_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> None:
    await article_service.delete_own_article(session, author_user_id=user_id, article_id=article_id)


@router.post(
    "/articles/{article_id}/blocks/{block_id}/image",
    response_model=ArticleBlockOut,
)
async def upload_article_block_image(
    article_id: UUID,
    block_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
) -> ArticleBlockOut:
    # Authorize before reading or persisting attacker-controlled bytes.
    await article_service.ensure_own_article_block(
        session, article_id=article_id, block_id=block_id, author_user_id=user_id
    )
    saved = await article_media.save_article_image(file, article_id=article_id)
    try:
        return await article_service.add_article_block_image(
            session,
            author_user_id=user_id,
            article_id=article_id,
            block_id=block_id,
            saved=saved,
        )
    except Exception:
        # Never leave bytes on disk that no row will ever reference.
        article_media.delete_article_image(saved.storage_key, article_id=article_id)
        raise


@router.delete(
    "/articles/{article_id}/blocks/{block_id}/image",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_article_block_image(
    article_id: UUID,
    block_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> None:
    await article_service.delete_article_block_image(
        session, author_user_id=user_id, article_id=article_id, block_id=block_id
    )


@router.get(
    "/articles/{article_id}/comments",
    response_model=ArticleCommentListOut,
)
async def list_article_comments(
    article_id: UUID,
    session: DbSession,
    viewer_user_id: OptionalCurrentUserId,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ArticleCommentListOut:
    return await article_comment_service.list_article_comments(
        session,
        article_id=article_id,
        viewer_user_id=viewer_user_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/articles/{article_id}/comments",
    response_model=ArticleCommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_article_comment(
    article_id: UUID,
    payload: ArticleCommentCreateIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> ArticleCommentOut:
    return await article_comment_service.create_article_comment(
        session, article_id=article_id, author_user_id=user_id, payload=payload
    )


@router.delete(
    "/articles/{article_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_article_comment(
    article_id: UUID,
    comment_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> None:
    await article_comment_service.delete_own_comment(
        session, article_id=article_id, comment_id=comment_id, author_user_id=user_id
    )
