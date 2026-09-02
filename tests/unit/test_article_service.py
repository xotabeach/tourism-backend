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
    ArticleLikeStatusOut,
    ArticleWriteIn,
)
from tourism_backend.modules.content.infrastructure.models import Article, ArticleBlock
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
    await session.execute(delete(Notification).where(Notification.user_id == user_id))
    await session.execute(delete(Article).where(Article.author_user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


@pytest.fixture
async def liker(session: AsyncSession) -> AsyncIterator[User]:
    """A second real user — liking writes a row with a genuine FK, unlike
    reads (get_article, list_related_articles), which tolerate a bare uuid4."""
    user = User(
        id=uuid4(),
        phone_e164=f"+7998{uuid4().int % 10_000_000:07d}",
        display_name="Читатель",
    )
    session.add(user)
    await session.commit()
    user_id = user.id
    yield user
    await session.rollback()
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


def test_quote_list_and_divider_block_shapes_are_validated() -> None:
    # A quote needs text; its caption is optional but only valid on a quote.
    quote = ArticleBlockIn(block_type="quote", text_content="Цитата", caption="Автор")
    assert quote.caption == "Автор"
    with pytest.raises(ValueError, match="quote blocks require text_content"):
        ArticleBlockIn(block_type="quote", text_content=None)
    with pytest.raises(ValueError, match="caption is only valid on quote blocks"):
        ArticleBlockIn(block_type="text", text_content="Текст", caption="Не сюда")

    # A list needs both text and a style; list_style elsewhere is rejected.
    bullets = ArticleBlockIn(
        block_type="list", text_content="Раз\nДва\nТри", list_style="bullet"
    )
    assert bullets.list_style == "bullet"
    with pytest.raises(ValueError, match="list blocks require text_content and list_style"):
        ArticleBlockIn(block_type="list", text_content="Раз", list_style=None)
    with pytest.raises(ValueError, match="list_style is only valid on list blocks"):
        ArticleBlockIn(block_type="text", text_content="Текст", list_style="bullet")
    with pytest.raises(ValueError, match="at most 15 items"):
        ArticleBlockIn(
            block_type="list",
            text_content="\n".join(f"Пункт {i}" for i in range(16)),
            list_style="numbered",
        )

    # A divider carries nothing at all.
    divider = ArticleBlockIn(block_type="divider")
    assert divider.text_content is None
    with pytest.raises(ValueError, match="divider blocks must not carry text_content"):
        ArticleBlockIn(block_type="divider", text_content="Не сюда")


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


@pytest.mark.asyncio
async def test_publishing_notifies_the_author_and_stamps_published_at(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)
    await article_service.submit_article_for_review(
        session, author_user_id=author.id, article_id=article_id
    )

    changed = await article_service.set_article_status(
        session, article_ids=[article_id], status="published"
    )
    assert changed == 1

    row = await session.get(Article, article_id)
    assert row is not None
    assert row.status == "published"
    # published_at is separate from moderated_at because the feed sorts by
    # when the article went live, and moderated_at is stamped on rejection too.
    assert row.published_at is not None
    assert row.moderated_at is not None

    inbox = (
        await session.scalars(
            select(Notification).where(
                Notification.user_id == author.id,
                Notification.target_id == article_id,
            )
        )
    ).all()
    assert [item.kind for item in inbox] == ["article_published"]


@pytest.mark.asyncio
async def test_rejecting_notifies_the_author_without_publishing(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)
    await article_service.submit_article_for_review(
        session, author_user_id=author.id, article_id=article_id
    )
    await article_service.set_article_status(session, article_ids=[article_id], status="rejected")

    row = await session.get(Article, article_id)
    assert row is not None
    assert row.status == "rejected"
    assert row.published_at is None

    inbox = (
        await session.scalars(
            select(Notification).where(
                Notification.user_id == author.id,
                Notification.target_id == article_id,
            )
        )
    ).all()
    assert [item.kind for item in inbox] == ["article_rejected"]

    # A rejected article goes back to the author to fix, not into limbo.
    reopened = await article_service.update_article_draft(
        session,
        author_user_id=author.id,
        article_id=article_id,
        payload=_payload(title="Доработано"),
    )
    assert reopened.title == "Доработано"


@pytest.mark.asyncio
async def test_moderating_an_already_published_article_changes_nothing(
    session: AsyncSession, author: User
) -> None:
    """Re-approving must not fire a second notification at the author."""
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)
    await article_service.submit_article_for_review(
        session, author_user_id=author.id, article_id=article_id
    )
    await article_service.set_article_status(session, article_ids=[article_id], status="published")

    again = await article_service.set_article_status(
        session, article_ids=[article_id], status="published"
    )
    assert again == 0

    inbox = (
        await session.scalars(
            select(Notification).where(
                Notification.user_id == author.id,
                Notification.target_id == article_id,
            )
        )
    ).all()
    assert len(inbox) == 1


async def _publish(session: AsyncSession, article_id: UUID) -> None:
    row = await session.get(Article, article_id)
    assert row is not None
    row.status = "published"
    row.published_at = datetime.now(UTC)
    await session.commit()


@pytest.mark.asyncio
async def test_excerpt_comes_from_the_first_text_block_only(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(
            blocks=[
                ArticleBlockIn(block_type="quote", text_content="Цитата не для превью"),
                ArticleBlockIn(block_type="text", text_content="Настоящий первый абзац."),
                ArticleBlockIn(block_type="text", text_content="Второй абзац."),
            ]
        ),
    )
    assert created.excerpt == "Настоящий первый абзац."


@pytest.mark.asyncio
async def test_excerpt_truncates_at_a_word_boundary(session: AsyncSession, author: User) -> None:
    long_text = " ".join(["слово"] * 60)  # 6 chars * 60 + 59 spaces > 200
    created = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(blocks=[ArticleBlockIn(block_type="text", text_content=long_text)]),
    )
    assert created.excerpt is not None
    assert created.excerpt.endswith("…")
    assert len(created.excerpt) <= 201
    assert not created.excerpt[:-1].endswith(" ")  # cut on a word, not mid-word


@pytest.mark.asyncio
async def test_reading_time_sums_prose_blocks_and_has_a_floor(
    session: AsyncSession, author: User
) -> None:
    tiny = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(blocks=[ArticleBlockIn(block_type="text", text_content="Коротко.")]),
    )
    assert tiny.reading_time_minutes == 1

    long_text = "слово " * 500  # 3000 chars
    long_article = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(
            title="Длинная",
            blocks=[
                ArticleBlockIn(block_type="text", text_content=long_text),
                ArticleBlockIn(
                    block_type="list", text_content="А\nБ", list_style="bullet"
                ),
            ],
        ),
    )
    # ~3000 + a couple chars of list content, at 800 chars/minute → ceil to 4.
    assert long_article.reading_time_minutes == 4


@pytest.mark.asyncio
async def test_like_toggle_is_idempotent_and_keeps_count_in_sync(
    session: AsyncSession, author: User, liker: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)
    await _publish(session, article_id)
    liker_id = liker.id

    first = await article_service.set_article_like(
        session, article_id=article_id, user_id=liker_id, liked=True
    )
    assert first == ArticleLikeStatusOut(like_count=1, liked_by_me=True)

    # Liking again must not double-count.
    again = await article_service.set_article_like(
        session, article_id=article_id, user_id=liker_id, liked=True
    )
    assert again == ArticleLikeStatusOut(like_count=1, liked_by_me=True)

    unliked = await article_service.set_article_like(
        session, article_id=article_id, user_id=liker_id, liked=False
    )
    assert unliked == ArticleLikeStatusOut(like_count=0, liked_by_me=False)

    # Unliking again must not go negative.
    unliked_again = await article_service.set_article_like(
        session, article_id=article_id, user_id=liker_id, liked=False
    )
    assert unliked_again == ArticleLikeStatusOut(like_count=0, liked_by_me=False)

    view = await article_service.get_article(
        session, article_id=article_id, viewer_user_id=liker_id
    )
    assert view.like_count == 0
    assert view.liked_by_me is False


@pytest.mark.asyncio
async def test_cannot_like_an_unpublished_or_missing_article(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    with pytest.raises(AppError) as exc:
        await article_service.set_article_like(
            session, article_id=UUID(created.id), user_id=uuid4(), liked=True
        )
    assert exc.value.code == "article_not_found"

    with pytest.raises(AppError):
        await article_service.set_article_like(
            session, article_id=uuid4(), user_id=uuid4(), liked=True
        )


@pytest.mark.asyncio
async def test_view_count_increments_only_for_published_articles(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)

    # The author viewing their own draft is not a "view" — never published.
    await article_service.get_article(session, article_id=article_id, viewer_user_id=author.id)
    row = await session.get(Article, article_id)
    assert row is not None
    assert row.view_count == 0

    await _publish(session, article_id)
    for _ in range(3):
        await article_service.get_article(
            session, article_id=article_id, viewer_user_id=uuid4()
        )
    row = await session.get(Article, article_id)
    assert row is not None
    assert row.view_count == 3


@pytest.mark.asyncio
async def test_saving_an_article_is_idempotent_and_shows_up_in_the_reading_list(
    session: AsyncSession, author: User, liker: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    article_id = UUID(created.id)
    await _publish(session, article_id)

    saved = await article_service.set_article_saved(
        session, article_id=article_id, user_id=liker.id, saved=True
    )
    assert saved.saved_by_me is True
    # Saving twice must not fail or duplicate the row.
    await article_service.set_article_saved(
        session, article_id=article_id, user_id=liker.id, saved=True
    )

    reading_list = await article_service.list_saved_articles(session, user_id=liker.id)
    assert [item.id for item in reading_list.items] == [created.id]
    assert reading_list.items[0].saved_by_me is True

    view = await article_service.get_article(
        session, article_id=article_id, viewer_user_id=liker.id
    )
    assert view.saved_by_me is True

    # ...and the author, who never saved it, does not see it as saved.
    others_view = await article_service.get_article(
        session, article_id=article_id, viewer_user_id=author.id
    )
    assert others_view.saved_by_me is False

    unsaved = await article_service.set_article_saved(
        session, article_id=article_id, user_id=liker.id, saved=False
    )
    assert unsaved.saved_by_me is False
    empty = await article_service.list_saved_articles(session, user_id=liker.id)
    assert empty.items == []


@pytest.mark.asyncio
async def test_an_unpublished_article_cannot_be_saved(
    session: AsyncSession, author: User, liker: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    with pytest.raises(AppError) as exc:
        await article_service.set_article_saved(
            session, article_id=UUID(created.id), user_id=liker.id, saved=True
        )
    assert exc.value.code == "article_not_found"


@pytest.mark.asyncio
async def test_related_articles_share_a_tag_and_exclude_self_and_untagged(
    session: AsyncSession, author: User
) -> None:
    main = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(title="Главная", tags=["История", "Пешком"]),
    )
    sibling = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(title="Тоже история", tags=["История"]),
    )
    unrelated = await article_service.create_article_draft(
        session,
        author_user_id=author.id,
        payload=_payload(title="Без тегов"),
    )
    for created in (main, sibling, unrelated):
        await _publish(session, UUID(created.id))

    related = await article_service.list_related_articles(
        session, article_id=UUID(main.id), viewer_user_id=None
    )
    ids = {item.id for item in related.items}
    assert sibling.id in ids
    assert main.id not in ids
    assert unrelated.id not in ids


@pytest.mark.asyncio
async def test_related_articles_is_empty_when_the_article_has_no_tags(
    session: AsyncSession, author: User
) -> None:
    created = await article_service.create_article_draft(
        session, author_user_id=author.id, payload=_payload()
    )
    await _publish(session, UUID(created.id))

    related = await article_service.list_related_articles(
        session, article_id=UUID(created.id), viewer_user_id=None
    )
    assert related.items == []
    assert related.total == 0


@pytest.mark.parametrize(
    ("block_type", "text_content", "list_style", "caption"),
    [
        # A list without list_style is malformed at the database, not just the schema.
        ("list", "Пункт", None, None),
        # A divider must not carry text.
        ("divider", "Текст", None, None),
        # list_style only belongs on a list block.
        ("quote", "Цитата", "bullet", None),
        # caption only belongs on a quote block.
        ("list", "Пункт", "bullet", "Подпись"),
    ],
)
@pytest.mark.asyncio
async def test_database_rejects_malformed_v2_block_shapes(
    session: AsyncSession,
    author: User,
    block_type: str,
    text_content: str | None,
    list_style: str | None,
    caption: str | None,
) -> None:
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
            list_style=list_style,
            caption=caption,
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
