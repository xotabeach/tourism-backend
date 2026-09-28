"""Route difficulty 1..5: estimate, reward estimate, manual rating (BACKEND-17, spec 17)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075_route_difficulty"
down_revision: str | Sequence[str] | None = "0074_transit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEVEL = "{column} IS NULL OR {column} BETWEEN 1 AND 5"


def upgrade() -> None:
    for column in (
        "difficulty_auto",
        "difficulty_reward",
        "difficulty_manual",
        "difficulty_level",
    ):
        op.add_column("routes", sa.Column(column, sa.SmallInteger(), nullable=True))
        op.create_check_constraint(f"ck_routes_{column}", "routes", _LEVEL.format(column=column))
    op.add_column("routes", sa.Column("difficulty_manual_by", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_routes_difficulty_manual_by",
        "routes",
        "difficulty_manual_by IS NULL OR difficulty_manual_by IN ('author', 'editorial', 'legacy')",
    )
    op.add_column("routes", sa.Column("difficulty_confidence", sa.String(8), nullable=True))
    op.add_column(
        "routes", sa.Column("difficulty_formula_version", sa.SmallInteger(), nullable=True)
    )
    op.create_index("ix_routes_difficulty_level", "routes", ["difficulty_level"])
    op.add_column("route_days", sa.Column("difficulty_level", sa.SmallInteger(), nullable=True))
    op.add_column(
        "route_routing_snapshots",
        sa.Column("difficulty_reward", sa.SmallInteger(), nullable=True),
    )
    # Ratings routes have today stay what people see until the owner has
    # seen the recalculation and switched them to the estimate (spec 17, 9).
    op.execute(
        """
        UPDATE routes SET
          difficulty_manual = lvl,
          difficulty_manual_by = 'legacy',
          difficulty_level = lvl
        FROM (
          SELECT id AS rid, CASE lower(trim(difficulty))
            WHEN 'easy' THEN 2 WHEN 'moderate' THEN 3 WHEN 'hard' THEN 4
            WHEN 'difficult' THEN 4 WHEN 'extreme' THEN 5 WHEN 'expert' THEN 5
          END AS lvl
          FROM routes
        ) AS legacy
        WHERE routes.id = legacy.rid AND legacy.lvl IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("route_routing_snapshots", "difficulty_reward")
    op.drop_column("route_days", "difficulty_level")
    op.drop_index("ix_routes_difficulty_level", table_name="routes")
    for column in (
        "difficulty_formula_version",
        "difficulty_confidence",
        "difficulty_manual_by",
        "difficulty_level",
        "difficulty_manual",
        "difficulty_reward",
        "difficulty_auto",
    ):
        op.drop_column("routes", column)
