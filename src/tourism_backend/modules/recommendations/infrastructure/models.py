"""Tables for daily recommendation decks and append-only feedback."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RouteRecommendationFeedback(Base, UUIDPrimaryKeyMixin):
    """Append-only skip (and later hide) events. No PII, no GPS."""

    __tablename__ = "route_recommendation_feedback"
    __table_args__ = (
        CheckConstraint(
            "action IN ('skip')",
            name="action_allowed",
        ),
        UniqueConstraint(
            "user_id",
            "client_event_id",
            name="uq_route_reco_feedback_user_event",
        ),
        Index(
            "ix_route_reco_feedback_user_route_created",
            "user_id",
            "route_id",
            "created_at",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    deck_date: Mapped[date] = mapped_column(Date, nullable=False)
    ranker_version: Mapped[str] = mapped_column(String(16), nullable=False)
    client_event_id: Mapped[UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class RouteRecommendationDeckItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One card in a user's Moscow-date deck for a ranker version."""

    __tablename__ = "route_recommendation_deck_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "deck_date",
            "ranker_version",
            "route_id",
            name="uq_route_reco_deck_user_date_version_route",
        ),
        CheckConstraint("rank >= 1", name="rank_positive"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_bounded"),
        Index(
            "ix_route_reco_deck_user_date_rank",
            "user_id",
            "deck_date",
            "ranker_version",
            "rank",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deck_date: Mapped[date] = mapped_column(Date, nullable=False)
    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    explanation_code: Mapped[str] = mapped_column(String(64), nullable=False)
    ranker_version: Mapped[str] = mapped_column(String(16), nullable=False)
    exploration: Mapped[bool] = mapped_column(nullable=False, default=False)
