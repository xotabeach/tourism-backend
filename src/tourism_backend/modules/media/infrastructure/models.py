"""Media attachment ORM models."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

ENTITY_TYPES = (
    "user",
    "place",
    "route",
    "review",
    "place_review",
    "support_ticket",
    "article",
)
ROLES = ("avatar", "cover", "gallery")
STATUSES = ("active", "archived")


class MediaAttachment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "media_attachments"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('user', 'place', 'route', 'review', 'place_review', "
            "'support_ticket', 'article')",
            name="entity_type",
        ),
        CheckConstraint(
            "role IN ('avatar', 'cover', 'gallery')",
            name="role",
        ),
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="status",
        ),
        Index("ix_media_attachments_entity", "entity_type", "entity_id", "role"),
        Index("ix_media_attachments_status", "status"),
        Index(
            "uq_media_attachments_one_avatar",
            "entity_type",
            "entity_id",
            unique=True,
            postgresql_where=text("role = 'avatar' AND status = 'active'"),
        ),
        Index(
            "uq_media_attachments_one_cover",
            "entity_type",
            "entity_id",
            unique=True,
            postgresql_where=text("role = 'cover' AND status = 'active'"),
        ),
    )

    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    public_path: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    uploaded_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
