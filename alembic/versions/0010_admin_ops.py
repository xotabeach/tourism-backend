"""Admin principals, roles, audit log; operator support author."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_admin_ops"
down_revision: str | Sequence[str] | None = "0009_user_notification_prefs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_principals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("login", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("login", name="uq_admin_principals_login"),
    )

    op.create_table(
        "admin_role_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["admin_principals.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "principal_id",
            "role",
            name="uq_admin_role_bindings_principal_role",
        ),
        sa.CheckConstraint("role IN ('ops', 'admin')", name="ck_admin_role_bindings_role"),
    )
    op.create_index(
        "ix_admin_role_bindings_principal_id",
        "admin_role_bindings",
        ["principal_id"],
    )

    op.create_table(
        "admin_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["admin_principals.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_audit_events_actor_id", "admin_audit_events", ["actor_id"])
    op.create_index("ix_admin_audit_events_created_at", "admin_audit_events", ["created_at"])

    op.drop_constraint("ck_support_messages_author", "support_messages", type_="check")
    op.create_check_constraint(
        "ck_support_messages_author",
        "support_messages",
        "author IN ('user', 'assistant', 'system', 'operator')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_support_messages_author", "support_messages", type_="check")
    op.create_check_constraint(
        "ck_support_messages_author",
        "support_messages",
        "author IN ('user', 'assistant', 'system')",
    )
    op.drop_index("ix_admin_audit_events_created_at", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_actor_id", table_name="admin_audit_events")
    op.drop_table("admin_audit_events")
    op.drop_index("ix_admin_role_bindings_principal_id", table_name="admin_role_bindings")
    op.drop_table("admin_role_bindings")
    op.drop_table("admin_principals")
