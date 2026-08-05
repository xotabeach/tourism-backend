"""Durable travel ranks assigned to users by TravelPoint thresholds."""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0014_travel_ranks"
down_revision: str | Sequence[str] | None = "0013_route_moderation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOVICE_ID = UUID("00000000-0000-0000-0000-000000000101")

RANKS = (
    (NOVICE_ID, "novice", "Новичок", 0, 1_000, 1),
    (UUID("00000000-0000-0000-0000-000000000102"), "traveler", "Путешественник", 1_000, 5_000, 2),
    (UUID("00000000-0000-0000-0000-000000000103"), "explorer", "Исследователь", 5_000, 10_000, 3),
    (
        UUID("00000000-0000-0000-0000-000000000104"),
        "advanced_hiker",
        "Продвинутый пешеход",
        10_000,
        25_000,
        4,
    ),
    (UUID("00000000-0000-0000-0000-000000000105"), "crimea_legend", "Легенда Крыма", 25_000, 0, 5),
)


def upgrade() -> None:
    ranks = op.create_table(
        "travel_ranks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("min_points", sa.Integer(), nullable=False),
        sa.Column("next_rank_points", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_travel_ranks"),
        sa.UniqueConstraint("slug", name="uq_travel_ranks_slug"),
        sa.UniqueConstraint("min_points", name="uq_travel_ranks_min_points"),
        sa.UniqueConstraint("sort_order", name="uq_travel_ranks_sort_order"),
    )
    op.bulk_insert(
        ranks,
        [
            {
                "id": rank_id,
                "slug": slug,
                "title": title,
                "min_points": min_points,
                "next_rank_points": next_points,
                "sort_order": sort_order,
            }
            for rank_id, slug, title, min_points, next_points, sort_order in RANKS
        ],
    )
    op.add_column("users", sa.Column("rank_id", sa.Uuid(), nullable=True))
    for rank_id, _slug, _title, min_points, _next, _order in reversed(RANKS):
        op.execute(
            sa.text(
                "UPDATE users SET rank_id = :rank_id "
                "WHERE rank_id IS NULL AND travel_points >= :min_points"
            ).bindparams(rank_id=rank_id, min_points=min_points)
        )
    op.alter_column(
        "users",
        "rank_id",
        existing_type=sa.Uuid(),
        nullable=False,
        server_default=str(NOVICE_ID),
    )
    op.create_foreign_key(
        "fk_users_rank_id_travel_ranks",
        "users",
        "travel_ranks",
        ["rank_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_users_rank_id", "users", ["rank_id"])


def downgrade() -> None:
    op.drop_index("ix_users_rank_id", table_name="users")
    op.drop_constraint("fk_users_rank_id_travel_ranks", "users", type_="foreignkey")
    op.drop_column("users", "rank_id")
    op.drop_table("travel_ranks")
