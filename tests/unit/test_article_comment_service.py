"""Comment authorship details (страница блога, макет 2026-09-04).

Ранг автора рядом с именем есть на макете и уже отдаётся у статьи —
у комментария читатель видел только имя.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.modules.content.application import article_comment_service, article_service
from tourism_backend.modules.content.application.article_schemas import (
    ArticleBlockIn,
    ArticleCommentCreateIn,
    ArticleWriteIn,
)
from tourism_backend.modules.content.infrastructure.models import Article, ArticleComment
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.notifications.infrastructure.models import Notification

DATABASE_URL = "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def author(session: AsyncSession) -> AsyncIterator[User]:
    user = User(
        id=uuid4(),
        phone_e164=f"+7997{uuid4().int % 10_000_000:07d}",
        display_name="Автор статьи",
    )
    session.add(user)
    await session.commit()
    user_id = user.id
    yield user
    await session.rollback()
    await session.execute(delete(Notification).where(Notification.user_id == user_id))
    await session.execute(delete(Article).where(Article.author_user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


@pytest.fixture
async def commenter(session: AsyncSession) -> AsyncIterator[User]:
    user = User(
        id=uuid4(),
        phone_e164=f"+7996{uuid4().int % 10_000_000:07d}",
        display_name="Комментатор",
    )
    session.add(user)
    await session.commit()
    user_id = user.id
    yield user
    await session.rollback()
    await session.execute(delete(ArticleComment).where(ArticleComment.author_user_id == user_id))
    await session.execute(delete(Notification).where(Notification.user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


@pytest.mark.asyncio
async def test_a_comment_carries_its_authors_rank(
    session: AsyncSession, author: User, commenter: User
) -> None:
    created = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=ArticleWriteIn(
            title="Не маршрут, а приключение",
            blocks=[ArticleBlockIn(block_type="text", text_content="Первый абзац.")],
        ),
    )
    article = await session.get(Article, created.id)
    assert article is not None
    article.status = "published"
    article.published_at = datetime.now(UTC)
    await session.commit()

    posted = await article_comment_service.create_article_comment(
        session,
        article_id=article.id,
        author_user_id=commenter.id,
        payload=ArticleCommentCreateIn(body="Отличный маршрут, повторили в июле!"),
    )
    assert posted.author_rank_title == "Новичок"

    # Тот же ранг приходит и в списке — свой ещё не одобренный комментарий
    # автор видит сразу.
    listed = await article_comment_service.list_article_comments(
        session,
        article_id=article.id,
        viewer_user_id=commenter.id,
    )
    assert [item.author_rank_title for item in listed.items] == ["Новичок"]
