"""Travel points, profile likes, favorite-route author awards."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_travel_points"
down_revision: str | Sequence[str] | None = "0010_admin_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "travel_points",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "profile_likes",
        sa.Column("liker_id", sa.Uuid(), nullable=False),
        sa.Column("liked_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("awarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["liker_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["liked_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("liker_id", "liked_user_id", name="pk_profile_likes"),
        sa.CheckConstraint("liker_id <> liked_user_id", name="ck_profile_likes_not_self"),
    )
    op.create_index("ix_profile_likes_liked_user_id", "profile_likes", ["liked_user_id"])
    op.add_column(
        "favorite_routes",
        sa.Column("author_points_awarded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("favorite_routes", "author_points_awarded_at")
    op.drop_index("ix_profile_likes_liked_user_id", table_name="profile_likes")
    op.drop_table("profile_likes")
    op.drop_column("users", "travel_points")
