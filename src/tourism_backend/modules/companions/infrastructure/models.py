"""Groups of companions walking one route together (spec 11b).

A group binds personal runs: every member walks their own run, with its own
points and anti-fraud; the group only says who walks with whom.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

GROUP_STATUSES = ("gathering", "started", "settled", "cancelled")

#: A member row from the first invitation to the end:
#: invited — waits for an answer (48 h); requested — came by code, waits for
#: the organizer; joined — in the group; declined, revoked (organizer took
#: the invitation back), rejected (organizer said no to a code request),
#: removed, left.
MEMBER_STATES = (
    "invited",
    "requested",
    "joined",
    "declined",
    "revoked",
    "rejected",
    "removed",
    "left",
)


class CompanionGroup(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "companion_groups"
    __table_args__ = (
        CheckConstraint(
            "status IN ('gathering', 'started', 'settled', 'cancelled')",
            name="status",
        ),
        CheckConstraint("max_members BETWEEN 2 AND 8", name="max_members"),
        # A code opens one live group at a time.
        Index(
            "uq_companion_groups_live_code",
            "join_code",
            unique=True,
            postgresql_where=text("status IN ('gathering', 'started')"),
        ),
        Index("ix_companion_groups_route", "route_id"),
    )

    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), nullable=False
    )
    organizer_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="gathering")
    #: The organizer's plan limit at creation (spec 11b D9).
    max_members: Mapped[int] = mapped_column(Integer, nullable=False)
    join_code: Mapped[str] = mapped_column(String(8), nullable=False)
    #: The route's content as the group walks it (D13): its
    #: ``content_updated_at`` when the group was made, renewed at the start.
    route_version_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CompanionMember(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "companion_members"
    __table_args__ = (
        CheckConstraint(
            "state IN ('invited', 'requested', 'joined', 'declined', 'revoked', "
            "'rejected', 'removed', 'left')",
            name="state",
        ),
        UniqueConstraint("group_id", "user_id", name="uq_companion_members_group_user"),
        Index("ix_companion_members_user_state", "user_id", "state"),
        Index("ix_companion_members_inviter_created", "invited_by_user_id", "created_at"),
    )

    group_id: Mapped[UUID] = mapped_column(
        ForeignKey("companion_groups.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    invited_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: An invitation answers within 48 hours (D3).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: «Готов» on the gathering screen (BACKEND-60).
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The member's own run once it is attached (BACKEND-60).
    execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_executions.id", ondelete="SET NULL"), nullable=True
    )
    joined_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Keeps the member off the group card that leaves the app (D17).
    hidden_from_card: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
