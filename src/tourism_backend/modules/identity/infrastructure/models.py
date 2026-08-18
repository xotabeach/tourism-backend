from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("phone_e164", name="uq_users_phone_e164"),)

    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    personal_data_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    notify_push_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    notify_sms_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    notify_haptics_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    travel_points: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    rank_id: Mapped[UUID] = mapped_column(
        ForeignKey("travel_ranks.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        default=UUID("00000000-0000-0000-0000-000000000101"),
        server_default="00000000-0000-0000-0000-000000000101",
    )


class TravelRank(Base):
    __tablename__ = "travel_ranks"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    min_points: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    next_rank_points: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)


class UserAchievement(Base):
    __tablename__ = "user_achievements"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    achievement_id: Mapped[UUID] = mapped_column(
        ForeignKey("achievements.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    unlocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProfileLike(Base):
    __tablename__ = "profile_likes"

    liker_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    liked_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthPhoneChangeChallenge(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "auth_phone_change_challenges"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Cleartext copy of the code, populated only while no SMS provider exists and
    # only in local/test (AUTH_OTP_STORE_DEBUG_CODE). Never set in staging/production.
    debug_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthOtpChallenge(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "auth_otp_challenges"

    phone_e164: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # See AuthPhoneChangeChallenge.debug_code.
    debug_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthRefreshSession(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "auth_refresh_sessions"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_auth_refresh_sessions_token_digest"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    family_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    device_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
