"""Persisted route runs and their stop snapshots."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RouteExecution(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One user's attempt to walk a route."""

    __tablename__ = "route_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed', 'cancelled')",
            name="status",
        ),
        Index("ix_route_executions_user_started", "user_id", "started_at"),
        Index(
            "uq_route_executions_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    route_name: Mapped[str] = mapped_column(String(255), nullable=False)
    route_cover_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RouteExecutionStop(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Stable stop snapshot belonging to one route execution."""

    __tablename__ = "route_execution_stops"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "position",
            name="uq_route_execution_stops_execution_position",
        ),
        CheckConstraint("position >= 1", name="position_positive"),
        Index("ix_route_execution_stops_execution_position", "execution_id", "position"),
    )

    execution_id: Mapped[UUID] = mapped_column(
        ForeignKey("route_executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_stop_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_stops.id", ondelete="SET NULL"),
        nullable=True,
    )
    place_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("places.id", ondelete="SET NULL"),
        nullable=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    place_name: Mapped[str] = mapped_column(String(255), nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_optional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
