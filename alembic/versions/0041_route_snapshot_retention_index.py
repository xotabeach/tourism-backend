"""Add an index for bounded routing snapshot retention jobs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_snapshot_retention"
down_revision: str | Sequence[str] | None = "0040_snapshot_immutable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_route_routing_snapshots_retention",
        "route_routing_snapshots",
        ["created_at", "route_id"],
    )
    op.execute(
        sa.text(
            "COMMENT ON INDEX ix_route_routing_snapshots_retention IS "
            "'Supports bounded deletion of old unreferenced routing snapshots'"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_route_routing_snapshots_retention",
        table_name="route_routing_snapshots",
    )
