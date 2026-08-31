"""Article lifecycle coverage against a live database (Workstream G, step 2).

Uses a real session rather than mocks because most of what is worth
checking here — the single-subject CHECK, the (article_id, position)
unique constraint, the block-shape CHECK — is enforced by Postgres, and
a mocked session would happily accept rows the real schema rejects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tourism_backend.api.errors import AppError
from tourism_backend.modules.content.application import article_service
from tourism_backend.modules.content.application.article_schemas import (
    ArticleBlockIn,
    ArticleWriteIn,
)
from tourism_backend.modules.content.infrastructure.models import Article, ArticleBlock
from tourism_backend.modules.identity.infrastructure.models import User

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
    """A throwaway author, plus removal of everything it wrote."""
    user = User(
        id=uuid4(),
        phone_e164=f"+7999{uuid4().int % 10_000_000:07d}",
        display_name="Автор статей",
    )
    session.add(user)
    await session.commit()
    # Hold the id as a plain value: a test that ends in a rolled-back
    # IntegrityError leaves `user` expired, and reading an attribute off it
    # during teardown would trigger a lazy refresh outside the async
    # context (MissingGreenlet) instead of cleaning up.
    user_id = user.id
    yield user
    await session.rollback()
    await session.execute(delete(Article).where(Article.author_user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


def _payload(title: str = "Как доехать до Ай-Петри", **kwargs: object) -> ArticleWriteIn:
    blocks = kwargs.pop("blocks", None)
    return ArticleWriteIn(
        title=title,
        blocks=blocks
        if blocks is not None
        else [ArticleBlockIn(block_type="text", text_content="Первый абзац.")],
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_create_draft_stores_blocks_in_order(session: AsyncSession, author: User) -> None:
    created = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(
            blocks=[
                ArticleBlockIn(block_type="text", text_content="Вступление"),
                ArticleBlockIn(block_type="image"),
                ArticleBlockIn(block_type="text", text_content="Заключение"),
            ]
        ),
    )

    assert created.status == "draft"
    assert [block.position for block in created.blocks] == [0, 1, 2]
    assert [block.block_type for block in created.blocks] == ["text", "image", "text"]
    # The image block is created empty on purpose — the file is uploaded
    # separately so a flaky connection can retry just the upload.
    assert created.blocks[1].image_url is None


@pytest.mark.asyncio
async def test_draft_is_hidden_from_strangers_but_visible_to_its_author(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)

    mine = await article_service.get_article(
        session, article_id=article_id, viewer_user_id=author.id
    )
    assert mine.id == created.id

    with pytest.raises(AppError) as exc:
        await article_service.get_article(session, article_id=article_id, viewer_user_id=uuid4())
    # 404 and not 403: whether a draft exists is not something a stranger
    # should be able to probe for.
    assert exc.value.status_code == 404
    assert exc.value.code == "article_not_found"


@pytest.mark.asyncio
async def test_update_replaces_the_whole_block_list(session: AsyncSession, author: User) -> None:
    created = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(
            blocks=[
                ArticleBlockIn(block_type="text", text_content="Старый текст"),
                ArticleBlockIn(block_type="text", text_content="Второй"),
            ]
        ),
    )
    updated = await article_service.update_article_draft(
        session,
        author_user_id=author.id,
        article_id=UUID(created.id),
        payload=_payload(
            title="Новый заголовок",
            blocks=[ArticleBlockIn(block_type="text", text_content="Единственный")],
        ),
    )

    assert updated.title == "Новый заголовок"
    assert len(updated.blocks) == 1
    assert updated.blocks[0].text_content == "Единственный"
    assert updated.blocks[0].position == 0


@pytest.mark.asyncio
async def test_article_under_moderation_cannot_be_edited(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)
    await article_service.submit_article_for_review(
        session, author_user_id=author.id, article_id=article_id
    )

    with pytest.raises(AppError) as exc:
        await article_service.update_article_draft(
            session,
            author_user_id=author.id,
            article_id=article_id,
            payload=_payload(title="Подмена после одобрения"),
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "article_not_editable"


@pytest.mark.asyncio
async def test_empty_article_cannot_be_submitted(session: AsyncSession, author: User) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload(blocks=[])
    )

    with pytest.raises(AppError) as exc:
        await article_service.submit_article_for_review(
            session, author_user_id=author.id, article_id=UUID(created.id)
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "article_empty"


@pytest.mark.asyncio
async def test_daily_submission_quota_is_enforced(session: AsyncSession, author: User) -> None:
    for index in range(3):
        created = await article_service.create_article_draft(
            session, author_user_id=author.id, payload=_payload(title=f"Статья {index}")
        )
        await article_service.submit_article_for_review(
            session, author_user_id=author.id, article_id=UUID(created.id)
        )

    over_limit = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload(title="Четвёртая")
    )
    with pytest.raises(AppError) as exc:
        await article_service.submit_article_for_review(
            session, author_user_id=author.id, article_id=UUID(over_limit.id)
        )
    assert exc.value.status_code == 429
    assert exc.value.code == "article_quota_exceeded"


@pytest.mark.asyncio
async def test_quota_only_counts_the_last_24_hours(session: AsyncSession, author: User) -> None:
    stale = datetime.now(UTC) - timedelta(hours=25)
    for index in range(3):
        created = await article_service.create_article_draft(
            session, author_user_id=author.id, payload=_payload(title=f"Вчера {index}")
        )
        row = await session.get(Article, UUID(created.id))
        assert row is not None
        row.status = "published"
        row.updated_at = stale
    await session.commit()

    fresh = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload(title="Сегодня")
    )
    submitted = await article_service.submit_article_for_review(
        session, author_user_id=author.id, article_id=UUID(fresh.id)
    )
    assert submitted.status == "pending_review"


@pytest.mark.asyncio
async def test_public_feed_shows_only_published_articles(
    session: AsyncSession, author: User
) -> None:
    draft = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload(title="Черновик")
    )
    published = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload(title="Опубликованная")
    )
    row = await session.get(Article, UUID(published.id))
    assert row is not None
    row.status = "published"
    row.published_at = datetime.now(UTC)
    await session.commit()

    feed = await article_service.list_published_articles(session, author_user_id=author.id)
    ids = {item.id for item in feed.items}
    assert published.id in ids
    assert draft.id not in ids

    mine = await article_service.list_my_articles(session, author_user_id=author.id)
    mine_ids = {item.id for item in mine.items}
    # The author's own workspace shows both.
    assert {draft.id, published.id} <= mine_ids


@pytest.mark.asyncio
async def test_deleted_article_leaves_the_authors_list(session: AsyncSession, author: User) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)
    await article_service.delete_own_article(
        session, author_user_id=author.id, article_id=article_id
    )

    mine = await article_service.list_my_articles(session, author_user_id=author.id)
    assert created.id not in {item.id for item in mine.items}

    row = await session.get(Article, article_id)
    assert row is not None
    # Soft delete: moderation history must survive the author's removal.
    assert row.status == "deleted"


@pytest.mark.asyncio
async def test_another_user_cannot_delete_someone_elses_article(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )

    with pytest.raises(AppError) as exc:
        await article_service.delete_own_article(
            session, author_user_id=uuid4(), article_id=UUID(created.id)
        )
    assert exc.value.status_code == 404

    row = await session.scalar(select(Article).where(Article.id == UUID(created.id)))
    assert row is not None
    assert row.status == "draft"


def test_article_cannot_target_a_route_and_a_place_at_once() -> None:
    with pytest.raises(ValueError, match="not both"):
        ArticleWriteIn(
            title="И туда, и сюда",
            related_route_id=uuid4(),
            related_place_id=uuid4(),
            blocks=[],
        )


def test_text_block_requires_text_and_image_block_forbids_it() -> None:
    with pytest.raises(ValueError, match="text blocks require text_content"):
        ArticleBlockIn(block_type="text", text_content="   ")
    with pytest.raises(ValueError, match="must not carry text_content"):
        ArticleBlockIn(block_type="image", text_content="подпись")


@pytest.mark.parametrize(
    ("block_type", "text_content", "attach_media"),
    [
        # A text block must never own an image...
        ("text", "Текст", True),
        # ...an image block must never own text...
        ("image", "Текст", False),
        # ...and a text block must never be empty.
        ("text", None, False),
    ],
)
@pytest.mark.asyncio
async def test_database_rejects_mixed_shape_blocks(
    session: AsyncSession,
    author: User,
    block_type: str,
    text_content: str | None,
    attach_media: bool,
) -> None:
    """The block_shape CHECK allows a pending image upload but nothing else.

    Guards the relaxation that lets an image block exist before its file
    does: only that one hole is open, every other mixed shape still fails
    at the database rather than reaching a renderer.
    """
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    session.add(
        ArticleBlock(
            id=uuid4(),
            article_id=UUID(created.id),
            position=99,
            block_type=block_type,
            text_content=text_content,
            media_attachment_id=uuid4() if attach_media else None,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
