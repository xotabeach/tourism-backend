"""Device-generated draft key so a retried draft save never duplicates the draft."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061_route_client_draft_id"
down_revision: str | Sequence[str] | None = "0060_route_antifraud"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("routes", sa.Column("client_draft_id", sa.String(length=36), nullable=True))
    op.create_index(
        "uq_routes_owner_client_draft",
        "routes",
        ["owner_user_id", "client_draft_id"],
        unique=True,
        postgresql_where=sa.text(
            "client_draft_id IS NOT NULL AND publication_status <> 'deleted'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_routes_owner_client_draft", table_name="routes")
    op.drop_column("routes", "client_draft_id")
