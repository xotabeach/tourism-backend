"""Admin-editable runtime settings that override static env config on read.

A generic key/value table rather than one bespoke table per flag —
Workstream B needs exactly one flag today (``ai_provider``), but more
admin-configurable toggles are already planned (see
``docs/ai-dual-provider-content-backlog-2026-08-31.md``, Workstream E), so
this stays reusable instead of growing a new migration per switch.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from tourism_backend.db.base import Base


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The ops principal (admin.infrastructure.models.AdminPrincipal) who
    # changed it — not an app User. This is only ever edited from /admin.
    updated_by_principal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("admin_principals.id", ondelete="SET NULL"), nullable=True
    )
