"""Car parks editors choose for stops of a route (BACKEND-24, spec 14b)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0073_parking_overrides"
down_revision: str | Sequence[str] | None = "0072_run_days"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # {place_id: [lng, lat]}: by place, like day breaks, so it survives the
    # stops being recreated.
    op.add_column(
        "routes",
        sa.Column("parking_overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("routes", "parking_overrides")
