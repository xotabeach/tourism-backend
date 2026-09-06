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


class CompanyDetails(Base):
    """Singleton row: legal/contact info shown on the mobile "О приложении"
    screen. A dedicated table rather than more runtime_settings rows — this
    is one structured record an admin fills in over time, not a set of
    independent toggles, and a real form (ModelView) beats editing 9 raw
    key/value rows one at a time.

    Public-read (GET /api/v1/company-details, no auth) — this is exactly the
    kind of content a guest reads before ever signing in.
    """

    __tablename__ = "company_details"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    legal_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    brand_name: Mapped[str] = mapped_column(String(64), nullable=False, default="КрымТрип")
    inn: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    ogrn: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    address: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    email: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    telegram: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    working_hours: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
