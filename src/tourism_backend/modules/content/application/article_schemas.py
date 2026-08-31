"""Wire schemas for articles (Workstream G).

Author fields are flattened the same way ``RouteReviewOut`` does it
(``author_display_name``/``author_avatar_url`` rather than a nested
object) so the mobile client can reuse the card it already renders for
review authors.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tourism_backend.modules.content.infrastructure.models import (
    MAX_BLOCKS_PER_ARTICLE,
    MAX_COMMENT_LENGTH,
    MAX_TEXT_BLOCK_LENGTH,
    MAX_TITLE_LENGTH,
)

ArticleStatus = Literal["draft", "pending_review", "published", "rejected", "deleted"]
ArticleCommentStatus = Literal["pending_review", "published", "rejected", "deleted"]
BlockType = Literal["text", "image"]


class ArticleBlockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_type: BlockType
    # Only text blocks carry content on the way in. An image block is
    # created empty and its file is uploaded separately (see G.2), so the
    # client can reserve block order first and retry just the upload on a
    # flaky connection instead of rebuilding the whole article.
    text_content: str | None = Field(default=None, max_length=MAX_TEXT_BLOCK_LENGTH)

    @field_validator("text_content")
    @classmethod
    def _trim_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @model_validator(mode="after")
    def _check_shape(self) -> "ArticleBlockIn":
        if self.block_type == "text" and not self.text_content:
            raise ValueError("text blocks require text_content")
        if self.block_type == "image" and self.text_content is not None:
            raise ValueError("image blocks must not carry text_content")
        return self


class ArticleBlockOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    position: int
    block_type: BlockType
    text_content: str | None = None
    image_url: str | None = None
    image_width: int | None = None
    image_height: int | None = None


class ArticleWriteIn(BaseModel):
    """Shared body for create and update — an edit replaces the article
    wholesale rather than patching individual blocks (see G.1)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    related_route_id: UUID | None = None
    related_place_id: UUID | None = None
    blocks: list[ArticleBlockIn] = Field(default_factory=list, max_length=MAX_BLOCKS_PER_ARTICLE)

    @field_validator("title")
    @classmethod
    def _trim_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _single_subject(self) -> "ArticleWriteIn":
        if self.related_route_id is not None and self.related_place_id is not None:
            raise ValueError("an article links to a route or a place, not both")
        return self


class ArticleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: ArticleStatus
    author_user_id: str
    author_display_name: str
    author_avatar_url: str | None
    related_route_id: str | None
    related_place_id: str | None
    cover_image_url: str | None
    moderator_note: str | None
    created_at: datetime
    published_at: datetime | None
    blocks: list[ArticleBlockOut] = Field(default_factory=list)


class ArticleSummaryOut(BaseModel):
    """Feed card — no blocks, so a listing never loads every article body."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: ArticleStatus
    author_user_id: str
    author_display_name: str
    author_avatar_url: str | None
    related_route_id: str | None
    related_place_id: str | None
    cover_image_url: str | None
    created_at: datetime
    published_at: datetime | None


class ArticleListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ArticleSummaryOut]
    total: int


class ArticleCommentCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MAX_COMMENT_LENGTH)
    reply_to_comment_id: UUID | None = None

    @field_validator("body")
    @classmethod
    def _trim_body(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned


class ArticleCommentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    article_id: str
    author_user_id: str
    author_display_name: str
    author_avatar_url: str | None
    body: str
    status: ArticleCommentStatus
    reply_to_comment_id: str | None
    created_at: datetime


class ArticleCommentListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ArticleCommentOut]
    total: int
