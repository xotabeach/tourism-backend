"""Route builder generation proposals and usage events."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RouteGenerationEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One counted generation attempt (form draft or chat proposal)."""

    __tablename__ = "route_generation_events"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('form', 'chat')",
            name="ck_route_generation_events_channel",
        ),
        Index("ix_route_generation_events_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_proposals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class RouteProposal(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Chat/form proposal before or as persisted generated draft."""

    __tablename__ = "route_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'accepted', 'rejected', 'superseded')",
            name="ck_route_proposals_status",
        ),
        CheckConstraint(
            "channel IN ('form', 'chat')",
            name="ck_route_proposals_channel",
        ),
        Index("ix_route_proposals_user_status", "user_id", "status"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    assistant_text: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    place_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    cover_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
