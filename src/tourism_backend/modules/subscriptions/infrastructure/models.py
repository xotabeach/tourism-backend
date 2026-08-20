"""Travel+ subscription persistence."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TravelPlusSubscription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Period-based Travel+ subscription row.

    ``users.travel_plus_active`` / ``expires_at`` / ``plan`` are denormalized
    for fast ``/me`` reads. This table is the durable history and the place
    where admin grants and mock checkouts are recorded. Store billing later
    adds a new ``source`` value without changing the read model.
    """

    __tablename__ = "travel_plus_subscriptions"
    __table_args__ = (
        CheckConstraint("plan IN ('monthly', 'yearly')", name="plan"),
        CheckConstraint(
            "status IN ('active', 'canceled', 'expired')",
            name="status",
        ),
        CheckConstraint(
            "source IN ('admin', 'mock_checkout')",
            name="source",
        ),
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        Index(
            "ix_travel_plus_subscriptions_user_active",
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
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by_principal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_principals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
