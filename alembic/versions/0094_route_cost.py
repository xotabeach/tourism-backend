"""A route's cost and price level (BACKEND-56, spec 33 D3/D4/D9/D12/D14).

``routes.cost`` keeps the breakdown (tickets, fares, fuel, nights, what is
missing) and ``routes.price_level`` the level from 1 to 4, or nothing while
the cost is not known. Both are recomputed by the backend; the first values
come from ``scripts/recalculate_route_costs.py`` after the deploy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0094_route_cost"
down_revision: str | Sequence[str] | None = "0093_price_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes", sa.Column("cost", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column("routes", sa.Column("price_level", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "price_level", "routes", "price_level IS NULL OR price_level BETWEEN 1 AND 4"
    )
    op.create_index("ix_routes_price_level", "routes", ["price_level"])


def downgrade() -> None:
    op.drop_index("ix_routes_price_level", table_name="routes")
    op.drop_constraint("price_level", "routes", type_="check")
    op.drop_column("routes", "price_level")
    op.drop_column("routes", "cost")
