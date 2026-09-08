"""Preserve proposal geometry and provisional trip plan before acceptance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055_proposal_preview"
down_revision: str | Sequence[str] | None = "0054_company_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("route_proposals", sa.Column("preview", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("route_proposals", "preview")
