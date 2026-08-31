"""In-app notification inbox rows."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, UUIDPrimaryKeyMixin


class Notification(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            "kind IN ("
            "'route_review', "
            "'route_published', "
            "'route_rejected', "
            "'review_published', "
            "'review_rejected', "
            "'profile_like', "
            "'achievement_unlocked', "
            "'support_reply', "
            "'review_reply', "
            "'expert_granted', "
            "'expert_revoked', "
            "'article_published', "
            "'article_rejected', "
            "'article_comment', "
            "'article_about_your_route'"
            ")",
            name="kind",
        ),
        CheckConstraint(
            "target_type IS NULL OR target_type IN "
            "('route', 'user', 'achievement', 'support_ticket', 'article')",
            name="target_type",
        ),
        Index("ix_notifications_inbox", "user_id", "is_read", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(String(500), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[UUID | None] = mapped_column(nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class DeviceToken(Base, UUIDPrimaryKeyMixin):
    """FCM registration token bound to a user (one row per device token)."""

    __tablename__ = "device_tokens"
    __table_args__ = (CheckConstraint("platform IN ('ios', 'android')", name="platform"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
