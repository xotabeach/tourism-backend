"""Index notifications by age so the retention purge does not scan the table."""

from collections.abc import Sequence

from alembic import op

revision: str = "0062_notifications_created_at"
down_revision: str | Sequence[str] | None = "0061_route_client_draft_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_created_at", table_name="notifications")
