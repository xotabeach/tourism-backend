"""Add recommendation feedback and daily deck tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_route_recommendations"
down_revision: str | Sequence[str] | None = "0041_snapshot_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_recommendation_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("deck_date", sa.Date(), nullable=False),
        sa.Column("ranker_version", sa.String(length=16), nullable=False),
        sa.Column("client_event_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('skip')", name="ck_route_recommendation_feedback_action"),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "client_event_id",
            name="uq_route_reco_feedback_user_event",
        ),
    )
    op.create_index(
        "ix_route_recommendation_feedback_user_id",
        "route_recommendation_feedback",
        ["user_id"],
    )
    op.create_index(
        "ix_route_recommendation_feedback_route_id",
        "route_recommendation_feedback",
        ["route_id"],
    )
    op.create_index(
        "ix_route_reco_feedback_user_route_created",
        "route_recommendation_feedback",
        ["user_id", "route_id", "created_at"],
    )

    op.create_table(
        "route_recommendation_deck_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("deck_date", sa.Date(), nullable=False),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("explanation_code", sa.String(length=64), nullable=False),
        sa.Column("ranker_version", sa.String(length=16), nullable=False),
        sa.Column("exploration", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rank >= 1", name="ck_route_recommendation_deck_items_rank"),
        sa.CheckConstraint(
            "score >= 0 AND score <= 1",
            name="ck_route_recommendation_deck_items_score",
        ),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "deck_date",
            "ranker_version",
            "route_id",
            name="uq_route_reco_deck_user_date_version_route",
        ),
    )
    op.create_index(
        "ix_route_recommendation_deck_items_user_id",
        "route_recommendation_deck_items",
        ["user_id"],
    )
    op.create_index(
        "ix_route_recommendation_deck_items_route_id",
        "route_recommendation_deck_items",
        ["route_id"],
    )
    op.create_index(
        "ix_route_reco_deck_user_date_rank",
        "route_recommendation_deck_items",
        ["user_id", "deck_date", "ranker_version", "rank"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_route_reco_deck_user_date_rank",
        table_name="route_recommendation_deck_items",
    )
    op.drop_index(
        "ix_route_recommendation_deck_items_route_id",
        table_name="route_recommendation_deck_items",
    )
    op.drop_index(
        "ix_route_recommendation_deck_items_user_id",
        table_name="route_recommendation_deck_items",
    )
    op.drop_table("route_recommendation_deck_items")
    op.drop_index(
        "ix_route_reco_feedback_user_route_created",
        table_name="route_recommendation_feedback",
    )
    op.drop_index(
        "ix_route_recommendation_feedback_route_id",
        table_name="route_recommendation_feedback",
    )
    op.drop_index(
        "ix_route_recommendation_feedback_user_id",
        table_name="route_recommendation_feedback",
    )
    op.drop_table("route_recommendation_feedback")
