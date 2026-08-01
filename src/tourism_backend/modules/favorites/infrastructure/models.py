from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base


class FavoritePlace(Base):
    __tablename__ = "favorite_places"
    __table_args__ = (PrimaryKeyConstraint("user_id", "place_id", name="pk_favorite_places"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    place_id: Mapped[UUID] = mapped_column(
        ForeignKey("places.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class FavoriteRoute(Base):
    __tablename__ = "favorite_routes"
    __table_args__ = (PrimaryKeyConstraint("user_id", "route_id", name="pk_favorite_routes"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    author_points_awarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
