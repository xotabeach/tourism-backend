"""Route planning chat sessions and messages (Phase 8B)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_route_planning_sessions"
down_revision: str | Sequence[str] | None = "0029_place_planning_enrichment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_planning_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("constraints", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'closed')",
            name="ck_route_planning_sessions_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_route_planning_sessions_user_id",
        "route_planning_sessions",
        ["user_id"],
    )
    op.create_index(
        "ix_route_planning_sessions_user_created",
        "route_planning_sessions",
        ["user_id", "created_at"],
    )

    op.create_table(
        "route_planning_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=True),
        sa.Column("proposal_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')",
            name="ck_route_planning_messages_role",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["route_planning_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["route_proposals.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_route_planning_messages_session_id",
        "route_planning_messages",
        ["session_id"],
    )
    op.create_index(
        "ix_route_planning_messages_user_id",
        "route_planning_messages",
        ["user_id"],
    )
    op.create_index(
        "ix_route_planning_messages_proposal_id",
        "route_planning_messages",
        ["proposal_id"],
    )
    op.create_index(
        "ix_route_planning_messages_session_created",
        "route_planning_messages",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_route_planning_messages_session_created",
        table_name="route_planning_messages",
    )
    op.drop_index(
        "ix_route_planning_messages_proposal_id",
        table_name="route_planning_messages",
    )
    op.drop_index(
        "ix_route_planning_messages_user_id",
        table_name="route_planning_messages",
    )
    op.drop_index(
        "ix_route_planning_messages_session_id",
        table_name="route_planning_messages",
    )
    op.drop_table("route_planning_messages")
    op.drop_index(
        "ix_route_planning_sessions_user_created",
        table_name="route_planning_sessions",
    )
    op.drop_index(
        "ix_route_planning_sessions_user_id",
        table_name="route_planning_sessions",
    )
    op.drop_table("route_planning_sessions")
