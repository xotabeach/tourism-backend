"""Routes: the «Море» tag (BACKEND-19).

A plain flag on the route. First marking, for an editor to check in the admin:
a user route whose author picked «Море» among the publish filters, and any
route with a stop in the «beach» category.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_route_seaside_tag"
down_revision: str | Sequence[str] | None = "0064_profile_preferences_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes",
        sa.Column("is_seaside", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        """
        UPDATE routes SET is_seaside = true
        WHERE accessibility -> 'filters' ? 'Море'
           OR id IN (
               SELECT rs.route_id
               FROM route_stops rs
               JOIN place_categories pc ON pc.place_id = rs.place_id
               JOIN categories c ON c.id = pc.category_id
               WHERE c.slug = 'beach'
           )
        """
    )


def downgrade() -> None:
    op.drop_column("routes", "is_seaside")
