"""Route reviews with moderation and in-app notifications."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_reviews_notifications"
down_revision: str | Sequence[str] | None = "0014_travel_ranks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("moderator_note", sa.String(length=500), nullable=True),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rating >= 1 AND rating <= 5",
            name=op.f("ck_route_reviews_rating_range"),
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'published', 'rejected', 'deleted')",
            name=op.f("ck_route_reviews_status"),
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name=op.f("fk_route_reviews_author_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["route_id"],
            ["routes.id"],
            name=op.f("fk_route_reviews_route_id_routes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_route_reviews")),
        sa.UniqueConstraint(
            "route_id",
            "author_user_id",
            name=op.f("uq_route_reviews_route_author"),
        ),
    )
    op.create_index(
        op.f("ix_route_reviews_route_id"),
        "route_reviews",
        ["route_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_route_reviews_author_user_id"),
        "route_reviews",
        ["author_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_route_reviews_route_status_created",
        "route_reviews",
        ["route_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_route_reviews_moderation_queue",
        "route_reviews",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('route_review')",
            name=op.f("ck_notifications_kind"),
        ),
        sa.CheckConstraint(
            "target_type IS NULL OR target_type IN ('route')",
            name=op.f("ck_notifications_target_type"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_notifications_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(
        op.f("ix_notifications_user_id"),
        "notifications",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_inbox",
        "notifications",
        ["user_id", "is_read", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_inbox", table_name="notifications")
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_route_reviews_moderation_queue", table_name="route_reviews")
    op.drop_index("ix_route_reviews_route_status_created", table_name="route_reviews")
    op.drop_index(op.f("ix_route_reviews_author_user_id"), table_name="route_reviews")
    op.drop_index(op.f("ix_route_reviews_route_id"), table_name="route_reviews")
    op.drop_table("route_reviews")
