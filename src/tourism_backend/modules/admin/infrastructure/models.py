"""Admin principals, roles, and audit events (ops panel only)."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

ADMIN_ROLES = ("ops", "admin")


class AdminPrincipal(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "admin_principals"
    __table_args__ = (UniqueConstraint("login", name="uq_admin_principals_login"),)

    login: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AdminRoleBinding(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "admin_role_bindings"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "role",
            name="uq_admin_role_bindings_principal_role",
        ),
        CheckConstraint("role IN ('ops', 'admin')", name="role"),
    )

    principal_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_principals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class AdminAuditEvent(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "admin_audit_events"

    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_principals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
