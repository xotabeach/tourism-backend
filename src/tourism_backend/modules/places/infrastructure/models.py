from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from tourism_backend.db.mixins import EditorialSourceMixin


class Category(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "categories"

    parent_category_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)


class Place(Base, UUIDPrimaryKeyMixin, TimestampMixin, EditorialSourceMixin):
    __tablename__ = "places"
    __table_args__ = (
        UniqueConstraint("region_id", "slug", name="uq_places_region_slug"),
        Index("ix_places_publication_region", "publication_status", "region_id"),
        Index("ix_places_name", "name"),
        Index("ix_places_payment_status", "payment_status"),
        Index("ix_places_difficulty", "difficulty"),
        Index(
            "uq_places_source_external_id",
            "source_name",
            "source_external_id",
            unique=True,
            postgresql_where=text("source_name IS NOT NULL AND source_external_id IS NOT NULL"),
        ),
    )

    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    locality_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("localities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(150), nullable=False)
    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    website_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    accessibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    recommended_equipment: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    seasonality: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    difficulty: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payment_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    price_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_suitable_for_children: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_suitable_for_pets: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recommended_visit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    safety_warnings: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    temporary_closure_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    temporary_closure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="published",
        index=True,
    )
    source_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_license: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    data_quality_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="needs_review",
        server_default="needs_review",
    )
    typical_crowding: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unknown",
        server_default="unknown",
        index=True,
    )
    price_min_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_max_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="RUB",
        server_default="RUB",
    )
    access_transport: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    parking_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    content_enrichment_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="missing",
        server_default="missing",
        index=True,
    )
    content_enrichment: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    proposed_slug: Mapped[str | None] = mapped_column(String(150), nullable=True)
    elevation_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Raw OSM `opening_hours` expression — untrusted free text, parsed at read
    # time; never the sole authority for whether a place is open.
    opening_hours_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    surface: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Set by scripts/dedupe_places.py when this place turned out to be an
    # OSM-imported duplicate of another (usually seed) place; the target
    # keeps its own row (routes.place_id is ondelete="RESTRICT") and takes
    # over its cover photo / typed fields instead of being deleted.
    merged_into_place_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class PlaceCategory(Base):
    __tablename__ = "place_categories"

    place_id: Mapped[UUID] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id: Mapped[UUID] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class PlaceEntrance(Base, UUIDPrimaryKeyMixin, TimestampMixin, EditorialSourceMixin):
    __tablename__ = "place_entrances"
    __table_args__ = (
        Index(
            "uq_place_entrances_one_primary",
            "place_id",
            unique=True,
            postgresql_where=text("is_primary IS TRUE AND status = 'active'"),
        ),
    )

    place_id: Mapped[UUID] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location = mapped_column(Geography(geometry_type="POINT", srid=4326), nullable=False)
    address_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    entrance_type: Mapped[str] = mapped_column(String(64), nullable=False, default="main")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    accessibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    vehicle_restrictions: Mapped[str | None] = mapped_column(Text, nullable=True)
    opening_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class PlaceSchedule(Base, UUIDPrimaryKeyMixin, TimestampMixin, EditorialSourceMixin):
    __tablename__ = "place_schedules"

    place_id: Mapped[UUID] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    place_entrance_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("place_entrances.id", ondelete="SET NULL"),
        nullable=True,
    )
    schedule_type: Mapped[str] = mapped_column(String(32), nullable=False, default="regular")
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    weekdays: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    opens_at: Mapped[time | None] = mapped_column(Time, nullable=True)
    closes_at: Mapped[time | None] = mapped_column(Time, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exception_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class PlaceImage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "place_images"
    __table_args__ = (
        Index(
            "uq_place_images_one_cover",
            "place_id",
            unique=True,
            postgresql_where=text("is_cover IS TRUE AND status = 'active'"),
        ),
    )

    place_id: Mapped[UUID] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    media_asset_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="photo")
    alt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    license: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_cover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class PlaceReview(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "place_reviews"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="rating_range"),
        CheckConstraint(
            "status IN ('pending_review', 'published', 'rejected', 'deleted')",
            name="status",
        ),
        Index("ix_place_reviews_place_status_created", "place_id", "status", "created_at"),
        Index("ix_place_reviews_moderation_queue", "status", "created_at"),
        Index(
            "ix_place_reviews_place_author_status",
            "place_id",
            "author_user_id",
            "status",
        ),
    )

    place_id: Mapped[UUID] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reply_to_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("place_reviews.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    body: Mapped[str] = mapped_column(String(2000), nullable=False)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    moderator_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoadEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Temporary road closures / restrictions for planning (SoT, not RAG)."""

    __tablename__ = "road_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'scheduled', 'resolved')",
            name="ck_road_events_status",
        ),
        CheckConstraint(
            "event_kind IN ('closure', 'restriction', 'congestion', 'other')",
            name="ck_road_events_kind",
        ),
        Index(
            "ix_road_events_source_external",
            "source_name",
            "source_external_id",
            unique=True,
            postgresql_where=text("source_name IS NOT NULL AND source_external_id IS NOT NULL"),
        ),
    )

    region_id: Mapped[UUID] = mapped_column(
        ForeignKey("regions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    affects_transport: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
