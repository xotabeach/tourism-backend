"""The app's error reports (BACKEND-73, spec 47).

Only signed-in people send them; they go with the account and are dropped
after 30 days by the app-version retention job. Operators (``ops``) may read
them; an admin may anyway.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0096_app_error_reports"
down_revision: str | Sequence[str] | None = "0095_bean_look"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_error_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("error_type", sa.String(120), nullable=False),
        sa.Column("stack", sa.Text(), nullable=True),
        sa.Column("screen", sa.String(120), nullable=True),
        sa.Column("app_version", sa.String(32), nullable=False),
        sa.Column("build_number", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("device_model", sa.String(80), nullable=True),
        sa.Column("os_version", sa.String(40), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('crash', 'cache_read', 'blank_screen')", name="kind"),
    )
    op.create_index("ix_app_error_reports_created", "app_error_reports", ["created_at"])
    op.create_index(
        "ix_app_error_reports_user_created", "app_error_reports", ["user_id", "created_at"]
    )
    grants = sa.table(
        "admin_role_permissions",
        sa.column("id", sa.Uuid()),
        sa.column("role", sa.String()),
        sa.column("permission", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        grants,
        [
            {
                "id": uuid4(),
                "role": "ops",
                "permission": "app_errors.read",
                "created_at": datetime.now(UTC),
            }
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM admin_role_permissions WHERE permission = 'app_errors.read'")
    op.execute("DELETE FROM admin_permission_overrides WHERE permission = 'app_errors.read'")
    op.drop_table("app_error_reports")
