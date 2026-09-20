"""Anonymous counters: APK downloads and active users per app version."""

from datetime import date
from uuid import UUID

from sqlalchemy import Date, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base


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
