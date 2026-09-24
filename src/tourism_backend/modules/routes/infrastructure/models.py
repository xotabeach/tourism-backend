from datetime import datetime
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from tourism_backend.db.mixins import EditorialSourceMixin


class Route(Base, UUIDPrimaryKeyMixin, TimestampMixin, EditorialSourceMixin):
    __tablename__ = "routes"
    __table_args__ = (
        UniqueConstraint("region_id", "slug", name="uq_routes_region_slug"),
        Index(
            "ix_routes_public_catalog",
            "source",
            "visibility",
            "lifecycle_status",
            "region_id",
        ),
        CheckConstraint(
            "source IN ('editorial', 'generated', 'user_created')",
            name="source",
        ),
        CheckConstraint(
            "visibility IN ('private', 'unlisted', 'public')",
            name="visibility",
        ),
        CheckConstraint(
            "lifecycle_status IN ('draft', 'active', 'archived')",
            name="lifecycle_status",
        ),
        CheckConstraint(
            "publication_status IN ('draft', 'pending_review', 'published', 'rejected', 'deleted')",
            name="publication_status",
        ),
        CheckConstraint("base_mode IN ('walk', 'car', 'mixed')", name="base_mode"),
        # One device-generated key per author's draft: a retry after a lost
        # response finds the draft it already created instead of making another.
        Index(
            "uq_routes_owner_client_draft",
            "owner_user_id",
            "client_draft_id",
            unique=True,
            postgresql_where=text(
                "client_draft_id IS NOT NULL AND publication_status <> 'deleted'"
            ),
        ),
        Index(
            "ix_routes_moderation_queue",
            "publication_status",
            "source",
            "updated_at",
        ),
    )

    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(150), nullable=False)
    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    publication_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="published",
        server_default="published",
        index=True,
    )
    estimated_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    budget_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    seasonality: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    transport_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_round_trip: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suitable_for_children: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pets_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The «Море» tag (BACKEND-19): a stop on a beach or by the shore. Worked
    # out from the stops (routes/application/seaside.py), an editor can fix
    # it in the admin.
    is_seaside: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    accessibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    geometry = mapped_column(Geography(geometry_type="LINESTRING", srid=4326), nullable=True)
    author_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    typical_crowding: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    price_min_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_max_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_draft_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Days and segments (spec 14). ``transport_mode`` keeps whatever spelling
    # clients sent; ``base_mode`` is its normalised walk/car/mixed.
    base_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="walk", server_default="walk"
    )
    needs_public_transport: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The author or an editor moved a day boundary: automatic splitting
    # leaves the days alone from then on (spec 14, D8).
    days_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Place ids after which the author or an editor ended a day, in route
    # order; stops themselves are recreated on every save (spec 14a).
    day_breaks: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    has_hard_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class RouteStop(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "route_stops"
    __table_args__ = (
        UniqueConstraint("route_id", "position", name="uq_route_stops_route_position"),
        CheckConstraint("position >= 1", name="position_positive"),
        CheckConstraint("time_of_day IN ('any', 'dark', 'dawn')", name="time_of_day"),
    )

    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    place_id: Mapped[UUID] = mapped_column(
        ForeignKey("places.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    place_entrance_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("place_entrances.id", ondelete="SET NULL"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    visit_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # «в тёмное время» / «к рассвету» (spec 14, D11).
    time_of_day: Mapped[str] = mapped_column(
        String(8), nullable=False, default="any", server_default="any"
    )


class RouteDay(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A continuous run of a route's stops walked or driven in one day."""

    __tablename__ = "route_days"
    __table_args__ = (
        UniqueConstraint("route_id", "day_index", name="uq_route_days_route_day"),
        CheckConstraint("day_index >= 1", name="day_index_positive"),
        CheckConstraint("boundary_source IN ('auto', 'manual')", name="boundary_source"),
    )

    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    first_stop_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_stops.id", ondelete="CASCADE"), nullable=False
    )
    last_stop_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_stops.id", ondelete="CASCADE"), nullable=False
    )
    boundary_source: Mapped[str] = mapped_column(
        String(8), nullable=False, default="auto", server_default="auto"
    )
    overnight_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    overloaded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    difficulty: Mapped[str | None] = mapped_column(String(32), nullable=True)


class RouteSegment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One stretch of a leg between two stops, travelled one way."""

    __tablename__ = "route_segments"
    __table_args__ = (
        UniqueConstraint("route_id", "leg_index", "seq", name="uq_route_segments_leg_seq"),
        CheckConstraint("leg_index >= 0 AND seq >= 0", name="order_non_negative"),
        CheckConstraint(
            "mode IN ('walk', 'car', 'bus', 'trolleybus', 'train', 'cable_car', 'ferry')",
            name="mode",
        ),
        CheckConstraint("role IN ('main', 'approach', 'return')", name="role"),
        CheckConstraint("origin IN ('router', 'synthetic', 'editor', 'transit')", name="origin"),
    )

    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    from_stop_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_stops.id", ondelete="CASCADE"), nullable=False
    )
    to_stop_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_stops.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="main")
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_gain_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elevation_loss_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geometry = mapped_column(Geography(geometry_type="LINESTRING", srid=4326), nullable=True)
    quality_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # A 12b transit line once public transport is routed; free text until then.
    transit_line_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class RouteReview(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "route_reviews"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="rating_range"),
        CheckConstraint(
            "status IN ('pending_review', 'published', 'rejected', 'deleted')",
            name="status",
        ),
        Index("ix_route_reviews_route_status_created", "route_id", "status", "created_at"),
        Index("ix_route_reviews_moderation_queue", "status", "created_at"),
        Index(
            "ix_route_reviews_route_author_status",
            "route_id",
            "author_user_id",
            "status",
        ),
    )

    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reply_to_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_reviews.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    body: Mapped[str] = mapped_column(String(2000), nullable=False)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    moderator_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
