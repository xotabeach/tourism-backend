"""Blocking a person and who may invite me (BACKEND-58, spec 11b D10, D15)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085_user_blocks"
down_revision: str | Sequence[str] | None = "0084_route_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_blocks",
        sa.Column(
            "blocker_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "blocked_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("blocker_id <> blocked_user_id", name="not_self"),
    )
    op.create_index("ix_user_blocks_blocked_user_id", "user_blocks", ["blocked_user_id"])
    op.add_column(
        "users",
        sa.Column("invite_policy", sa.String(16), nullable=False, server_default="mutual"),
    )
    op.create_check_constraint("invite_policy", "users", "invite_policy IN ('mutual', 'nobody')")


def downgrade() -> None:
    op.drop_constraint("invite_policy", "users", type_="check")
    op.drop_column("users", "invite_policy")
    op.drop_index("ix_user_blocks_blocked_user_id", table_name="user_blocks")
    op.drop_table("user_blocks")
