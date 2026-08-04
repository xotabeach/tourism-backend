"""Add moderation workflow for user-created routes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_route_moderation"
down_revision: str | Sequence[str] | None = "0012_support_ticket_last_human"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "routes",
        sa.Column(
            "publication_status",
            sa.String(length=32),
            nullable=False,
            server_default="published",
        ),
    )
    op.create_check_constraint(
        "ck_routes_publication_status",
        "routes",
        "publication_status IN ('draft', 'pending_review', 'published', 'rejected', 'deleted')",
    )
    op.create_index(
        "ix_routes_publication_status",
        "routes",
        ["publication_status"],
    )
    op.create_index(
        "ix_routes_moderation_queue",
        "routes",
        ["publication_status", "source", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_routes_moderation_queue", table_name="routes")
    op.drop_index("ix_routes_publication_status", table_name="routes")
    op.drop_constraint("ck_routes_publication_status", "routes", type_="check")
    op.drop_column("routes", "publication_status")
