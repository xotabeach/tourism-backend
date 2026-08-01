"""Persist admin audit events."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.admin.infrastructure.models import AdminAuditEvent


async def record_audit(
    session: AsyncSession,
    *,
    actor_id: UUID | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
    detail: str | None = None,
    commit: bool = False,
) -> None:
    session.add(
        AdminAuditEvent(
            id=uuid4(),
            actor_id=actor_id,
            action=action[:64],
            entity_type=entity_type[:64],
            entity_id=entity_id[:64] if entity_id else None,
            metadata_json=metadata,
            ip=ip[:64] if ip else None,
            detail=detail,
            created_at=datetime.now(UTC),
        )
    )
    if commit:
        await session.commit()
    else:
        await session.flush()
