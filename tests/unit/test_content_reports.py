"""Жалобы на контент.

Жалоба ничего не скрывает сама: она ставит объект в очередь модерации.
Проверяем ровно это — плюс защиту от повторов и от жалоб на самого себя.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.api.errors import AppError
from tourism_backend.modules.content.application import article_comment_service, article_service
from tourism_backend.modules.content.application.article_schemas import (
    ArticleBlockIn,
    ArticleCommentCreateIn,
    ArticleWriteIn,
)
from tourism_backend.modules.content.infrastructure.models import Article, ArticleComment
from tourism_backend.modules.identity.infrastructure.models import User
from tourism_backend.modules.moderation.application import service as moderation_service
from tourism_backend.modules.moderation.application.schemas import ContentReportCreateIn
from tourism_backend.modules.moderation.infrastructure.models import ContentReport
from tourism_backend.modules.notifications.infrastructure.models import Notification

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tourism:local-tourism-password@localhost:5433/tourism",
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _user(session: AsyncSession, prefix: str, name: str) -> User:
    user = User(
        id=uuid4(),
        phone_e164=f"+{prefix}{uuid4().int % 10_000_000:07d}",
        display_name=name,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def author(session: AsyncSession) -> AsyncIterator[User]:
    user = await _user(session, "7995", "Автор")
    user_id = user.id
    yield user
    await session.rollback()
    await session.execute(delete(Notification).where(Notification.user_id == user_id))
    await session.execute(delete(Article).where(Article.author_user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


@pytest.fixture
async def reader(session: AsyncSession) -> AsyncIterator[User]:
    user = await _user(session, "7994", "Читатель")
    user_id = user.id
    yield user
    await session.rollback()
    await session.execute(delete(ContentReport).where(ContentReport.reporter_user_id == user_id))
    await session.execute(delete(ArticleComment).where(ArticleComment.author_user_id == user_id))
    await session.execute(delete(Notification).where(Notification.user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


async def _published_article(session: AsyncSession, author: User) -> Article:
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
    return article


@pytest.mark.asyncio
async def test_a_report_lands_in_the_queue_and_repeats_do_not_pile_up(
    session: AsyncSession, author: User, reader: User
) -> None:
    article = await _published_article(session, author)
    comment = await article_comment_service.create_article_comment(
        session,
        article_id=article.id,
        author_user_id=author.id,
        payload=ArticleCommentCreateIn(body="Купите курсы по ссылке в профиле"),
    )

    first = await moderation_service.create_report(
        session,
        reporter_user_id=reader.id,
        payload=ContentReportCreateIn(
            target_type="article_comment",
            target_id=comment.id,
            reason="spam",
            comment="Реклама курсов",
        ),
    )
    assert first.status == "new"
    assert first.already_reported is False

    # Повторное нажатие возвращает ту же жалобу, а не заводит вторую.
    again = await moderation_service.create_report(
        session,
        reporter_user_id=reader.id,
        payload=ContentReportCreateIn(
            target_type="article_comment",
            target_id=comment.id,
            reason="abuse",
        ),
    )
    assert again.id == first.id
    assert again.already_reported is True
    assert again.reason == "spam"

    # Комментарий остаётся опубликованным: жалоба его не скрывает.
    stored = await session.get(ArticleComment, comment.id)
    assert stored is not None
    assert stored.status == "pending_review"

    await session.execute(delete(ContentReport).where(ContentReport.id == first.id))
    await session.commit()


@pytest.mark.asyncio
async def test_you_cannot_report_your_own_comment(session: AsyncSession, author: User) -> None:
    article = await _published_article(session, author)
    comment = await article_comment_service.create_article_comment(
        session,
        article_id=article.id,
        author_user_id=author.id,
        payload=ArticleCommentCreateIn(body="Свой же комментарий"),
    )
    with pytest.raises(AppError) as error:
        await moderation_service.create_report(
            session,
            reporter_user_id=author.id,
            payload=ContentReportCreateIn(
                target_type="article_comment",
                target_id=comment.id,
                reason="spam",
            ),
        )
    assert error.value.code == "report_own_content"


@pytest.mark.asyncio
async def test_a_report_on_something_that_does_not_exist_is_a_404(
    session: AsyncSession, reader: User
) -> None:
    with pytest.raises(AppError) as error:
        await moderation_service.create_report(
            session,
            reporter_user_id=reader.id,
            payload=ContentReportCreateIn(
                target_type="article",
                target_id=str(uuid4()),
                reason="other",
            ),
        )
    assert error.value.code == "report_target_not_found"


@pytest.mark.asyncio
async def test_admin_moves_reports_through_statuses(
    session: AsyncSession, author: User, reader: User
) -> None:
    article = await _published_article(session, author)
    report = await moderation_service.create_report(
        session,
        reporter_user_id=reader.id,
        payload=ContentReportCreateIn(
            target_type="article",
            target_id=str(article.id),
            reason="copyright",
        ),
    )
    from uuid import UUID as _UUID

    changed = await moderation_service.set_report_status(
        session,
        report_ids=[_UUID(report.id)],
        status="in_review",
    )
    await session.commit()
    assert changed == 1

    # Повторный перевод в тот же статус ничего не меняет.
    changed_again = await moderation_service.set_report_status(
        session,
        report_ids=[_UUID(report.id)],
        status="in_review",
    )
    await session.commit()
    assert changed_again == 0

    stored = await session.get(ContentReport, _UUID(report.id))
    assert stored is not None
    assert stored.status == "in_review"

    await session.execute(delete(ContentReport).where(ContentReport.id == stored.id))
    await session.commit()
