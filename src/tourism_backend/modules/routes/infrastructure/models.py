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


class RouteStop(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "route_stops"
    __table_args__ = (
        UniqueConstraint("route_id", "position", name="uq_route_stops_route_position"),
        CheckConstraint("position >= 1", name="position_positive"),
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
