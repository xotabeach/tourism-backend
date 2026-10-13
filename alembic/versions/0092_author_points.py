"""An author's own points inside a route (BACKEND-43, spec 15 D10/D18).

An author point is a place that lives only in its route: publication status
``route_only``, the route it belongs to and its kind. The catalogue lists
published places only, so it never shows there; the editors can promote a
good one by publishing it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0092_author_points"
down_revision: str | Sequence[str] | None = "0091_route_mountain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "places",
        sa.Column(
            "author_route_id",
            sa.Uuid(),
            sa.ForeignKey("routes.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("places", sa.Column("author_point_kind", sa.String(16), nullable=True))
    op.create_index("ix_places_author_route_id", "places", ["author_route_id"])


def downgrade() -> None:
    op.drop_index("ix_places_author_route_id", table_name="places")
    op.drop_column("places", "author_point_kind")
    op.drop_column("places", "author_route_id")
