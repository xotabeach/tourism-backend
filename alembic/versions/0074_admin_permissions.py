"""Role grants and per-employee permission exceptions (BACKEND-9)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0074_admin_permissions"
down_revision: str | Sequence[str] | None = "0073_parking_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DEFAULT_GRANTS = {
    "ops": ("support.read", "support.write", "users.brief"),
    "support": ("support.read", "support.write", "users.brief"),
    "route_manager": (
        "routes.read",
        "routes.write",
        "places.read",
        "places.write",
        "geography.read",
        "geography.write",
        "reviews.read",
        "reviews.write",
        "recommendations.read",
        "recommendations.write",
        "moderation_route.read",
        "moderation_route.write",
        "moderation.read",
        "moderation.write",
    ),
    "content_manager": (
        "content.read",
        "content.write",
        "media.read",
        "media.write",
        "moderation_content.read",
        "moderation_content.write",
        "moderation.read",
        "moderation.write",
    ),
}


def upgrade() -> None:
    op.drop_constraint("ck_admin_role_bindings_role", "admin_role_bindings", type_="check")
    op.create_check_constraint(
        "ck_admin_role_bindings_role",
        "admin_role_bindings",
        "role IN ('admin', 'ops', 'support', 'route_manager', 'content_manager')",
    )
    op.create_table(
        "admin_role_permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("role", "permission", name="uq_admin_role_permission"),
        sa.CheckConstraint(
            "role IN ('ops', 'support', 'route_manager', 'content_manager')",
            name="ck_admin_role_permissions_role",
        ),
    )
    op.create_index("ix_admin_role_permissions_role", "admin_role_permissions", ["role"])
    op.create_table(
        "admin_permission_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("effect", sa.String(length=5), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["admin_principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("principal_id", "permission", name="uq_admin_permission_override"),
        sa.CheckConstraint(
            "effect IN ('allow', 'deny')", name="ck_admin_permission_overrides_effect"
        ),
    )
    op.create_index(
        "ix_admin_permission_overrides_principal_id", "admin_permission_overrides", ["principal_id"]
    )

    grants = sa.table(
        "admin_role_permissions",
        sa.column("id", sa.Uuid()),
        sa.column("role", sa.String()),
        sa.column("permission", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        grants,
        [
            {"id": uuid4(), "role": role, "permission": permission, "created_at": now}
            for role, permissions in _DEFAULT_GRANTS.items()
            for permission in permissions
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_admin_permission_overrides_principal_id", table_name="admin_permission_overrides"
    )
    op.drop_table("admin_permission_overrides")
    op.drop_index("ix_admin_role_permissions_role", table_name="admin_role_permissions")
    op.drop_table("admin_role_permissions")
    op.execute("DELETE FROM admin_role_bindings WHERE role NOT IN ('ops', 'admin')")
    op.drop_constraint("ck_admin_role_bindings_role", "admin_role_bindings", type_="check")
    op.create_check_constraint(
        "ck_admin_role_bindings_role", "admin_role_bindings", "role IN ('ops', 'admin')"
    )
