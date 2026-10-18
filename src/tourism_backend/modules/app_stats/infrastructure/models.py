"""Anonymous counters (APK downloads, active users per app version) and the
app's error reports."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, UUIDPrimaryKeyMixin


class ApkDownloadDaily(Base):
    """One row per (Moscow day, source, apk version). No IP, no User-Agent."""

    __tablename__ = "apk_download_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    apk_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AppVersionDailyUser(Base):
    """Who was active on which app version; kept 35 days, then only aggregates."""

    __tablename__ = "app_version_daily_users"
    __table_args__ = (Index("ix_app_version_daily_users_day", "day"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    app_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    build_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), primary_key=True)


class AppVersionDaily(Base):
    """Users per day and version, kept for good."""

    __tablename__ = "app_version_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    app_version: Mapped[str] = mapped_column(String(32), primary_key=True)
    build_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), primary_key=True)
    users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AppErrorReport(Base, UUIDPrimaryKeyMixin):
    """One error the app reported (BACKEND-73, spec 47).

    Only what helps to find the bug: the kind, the error's type, the stack,
    the screen and the device. No phone, name, tokens or typed text; for a
    stored entry that would not read, only which store and the error type.
    Kept 30 days, and goes with the account.
    """

    __tablename__ = "app_error_reports"
    __table_args__ = (
        CheckConstraint("kind IN ('crash', 'cache_read', 'blank_screen')", name="kind"),
        Index("ix_app_error_reports_created", "created_at"),
        Index("ix_app_error_reports_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    error_type: Mapped[str] = mapped_column(String(120), nullable=False)
    stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    screen: Mapped[str | None] = mapped_column(String(120), nullable=True)
    app_version: Mapped[str] = mapped_column(String(32), nullable=False)
    build_number: Mapped[int] = mapped_column(Integer, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    device_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
