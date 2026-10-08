"""Place view events and popularity of places and routes (BACKEND-31, spec 19)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080_popularity"
down_revision: str | Sequence[str] | None = "0079_route_cover_image"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "place_view_events",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("place_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_place_view_events_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["place_id"],
            ["places.id"],
            name="fk_place_view_events_place_id_places",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "place_id", "day", name="pk_place_view_events"),
    )
    op.create_index("ix_place_view_events_place_day", "place_view_events", ["place_id", "day"])
    op.create_index("ix_place_view_events_day", "place_view_events", ["day"])

    op.add_column(
        "users",
        sa.Column("is_internal_account", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column("places", sa.Column("popularity_external", sa.Float(), nullable=True))
    for table in ("places", "routes"):
        op.add_column(
            table,
            sa.Column("popularity", sa.Float(), nullable=False, server_default="0"),
        )
        op.add_column(
            table,
            sa.Column("popularity_people", sa.Integer(), nullable=False, server_default="0"),
        )
        op.add_column(
            table,
            sa.Column("is_popular", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.add_column(
            table,
            sa.Column("popularity_updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for table in ("routes", "places"):
        op.drop_column(table, "popularity_updated_at")
        op.drop_column(table, "is_popular")
        op.drop_column(table, "popularity_people")
        op.drop_column(table, "popularity")
    op.drop_column("places", "popularity_external")
    op.drop_column("users", "is_internal_account")
    op.drop_index("ix_place_view_events_day", table_name="place_view_events")
    op.drop_index("ix_place_view_events_place_day", table_name="place_view_events")
    op.drop_table("place_view_events")
