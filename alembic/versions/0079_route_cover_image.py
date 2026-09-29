"""Route cover chosen from a place photo (BACKEND-16, spec 16a D33, D37)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0079_route_cover_image"
down_revision: str | Sequence[str] | None = "0078_system_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes",
        sa.Column("cover_place_image_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # SET NULL: a removed photo falls back to the first stop's cover.
    op.create_foreign_key(
        "fk_routes_cover_place_image_id_place_images",
        "routes",
        "place_images",
        ["cover_place_image_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_routes_cover_place_image_id", "routes", ["cover_place_image_id"])


def downgrade() -> None:
    op.drop_index("ix_routes_cover_place_image_id", table_name="routes")
    op.drop_constraint("fk_routes_cover_place_image_id_place_images", "routes", type_="foreignkey")
    op.drop_column("routes", "cover_place_image_id")
