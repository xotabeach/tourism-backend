"""Manual day boundaries by place, and the automatic day count of a run (BACKEND-24)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0071_manual_day_breaks"
down_revision: str | Sequence[str] | None = "0070_parkings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stops are recreated on every author save, so a boundary is kept by the
    # place it follows, not by the stop row (spec 14a, section 2).
    op.add_column(
        "routes",
        sa.Column("day_breaks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # Nullable addition: does not fire the snapshot UPDATE trigger.
    op.add_column(
        "route_routing_snapshots",
        sa.Column("auto_day_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("route_routing_snapshots", "auto_day_count")
    op.drop_column("routes", "day_breaks")
