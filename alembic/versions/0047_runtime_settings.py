"""Add runtime_settings table for admin-editable config overrides (Workstream B)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_runtime_settings"
down_revision: str | Sequence[str] | None = "0046_achievement_card"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=256), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # The ops principal who changed it (admin/presentation session), not
        # an app User — this setting is only ever edited from /admin.
        sa.Column("updated_by_principal_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_principal_id"], ["admin_principals.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
