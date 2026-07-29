"""Favorite places and routes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_favorites"
down_revision: str | Sequence[str] | None = "0004_identity_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "favorite_places",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["place_id"], ["places.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "place_id", name="pk_favorite_places"),
    )
    op.create_index("ix_favorite_places_place_id", "favorite_places", ["place_id"])

    op.create_table(
        "favorite_routes",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "route_id", name="pk_favorite_routes"),
    )
    op.create_index("ix_favorite_routes_route_id", "favorite_routes", ["route_id"])


def downgrade() -> None:
    op.drop_table("favorite_routes")
    op.drop_table("favorite_places")
