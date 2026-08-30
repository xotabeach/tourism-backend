"""Route builder generation proposals and usage events."""

from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
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


class RoutePlanningSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Travel+ AI chat session with typed travel constraints."""

    __tablename__ = "route_planning_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'closed')",
            name="ck_route_planning_sessions_status",
        ),
        Index("ix_route_planning_sessions_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confirmed_fields: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )


class RoutePlanningMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One turn in a planning session (user or assistant)."""

    __tablename__ = "route_planning_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="ck_route_planning_messages_role",
        ),
        Index(
            "ix_route_planning_messages_session_created",
            "session_id",
            "created_at",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_planning_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_proposals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class RouteTerrainFeature(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Independent OSM/Overpass geometry for the segment-level terrain gate.

    Coastline and trail ways for Crimea, imported once via
    ``scripts/import_terrain_features.py`` (same Overpass source used for
    the places catalog). Read-only for route generation; never edited from
    the app. Not a field survey — see route_quality.py for how findings from
    this table stay "review"/"warning", never a hard safety claim.
    """

    __tablename__ = "route_terrain_features"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('coastline', 'trail')",
            name="ck_route_terrain_features_kind",
        ),
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    geometry = mapped_column(Geography(geometry_type="LINESTRING", srid=4326), nullable=False)
    source_osm_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
