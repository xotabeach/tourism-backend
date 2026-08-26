"""Add travel-preferences quiz answers to users.

Backs the "Сменить предпочтения" settings item, which used to be a
placeholder ("Тест предпочтений появится позже") with nowhere to store an
answer.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036_user_travel_preferences"
down_revision: str | Sequence[str] | None = "0035_support_ticket_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("preferred_categories", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("preferred_difficulty", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "travels_with_kids",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "travels_with_pets",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("preferences_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "preferred_difficulty",
        "users",
        "preferred_difficulty IS NULL OR preferred_difficulty IN ('easy', 'moderate', 'hard')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_preferred_difficulty", "users", type_="check")
    op.drop_column("users", "preferences_updated_at")
    op.drop_column("users", "travels_with_pets")
    op.drop_column("users", "travels_with_kids")
    op.drop_column("users", "preferred_difficulty")
    op.drop_column("users", "preferred_categories")
