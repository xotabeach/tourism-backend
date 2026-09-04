"""Жалобы на пользовательский контент.

Отдельный модуль, а не угол ``content``: пожаловаться можно и на маршрут, и
на место, и на комментарий — общая очередь модерации не принадлежит ни
одному из этих агрегатов.
"""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

REPORT_TARGET_TYPES = ("article_comment", "article", "route", "place")

# Причины — это то, что человек выбирает в приложении. Формулировки короткие:
# длинный список читается дольше, чем пишется сама жалоба.
REPORT_REASONS = (
    "spam",
    "abuse",
    "inappropriate",
    "misinformation",
    "copyright",
    "other",
)
REPORT_STATUSES = ("new", "in_review", "resolved", "rejected")

MAX_REPORT_COMMENT_LENGTH = 500


class ContentReport(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "content_reports"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('article_comment', 'article', 'route', 'place')",
            name="ck_content_reports_target",
        ),
        CheckConstraint(
            "reason IN ('spam', 'abuse', 'inappropriate', 'misinformation', 'copyright', 'other')",
            name="ck_content_reports_reason",
        ),
        CheckConstraint(
            "status IN ('new', 'in_review', 'resolved', 'rejected')",
            name="ck_content_reports_status",
        ),
        UniqueConstraint(
            "target_type",
            "target_id",
            "reporter_user_id",
            name="uq_content_reports_target_reporter",
        ),
        Index("ix_content_reports_status_created", "status", "created_at"),
        Index("ix_content_reports_target", "target_type", "target_id"),
    )

    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    reporter_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(MAX_REPORT_COMMENT_LENGTH), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="new")
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resolved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
