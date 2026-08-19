"""Operator support replies (not mobile JWT path)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.config import Settings
from tourism_backend.modules.admin.application.audit import record_audit
from tourism_backend.modules.notifications.application import service as notifications_service
from tourism_backend.modules.support.infrastructure.models import SupportMessage, SupportTicket

_MAX_BODY = 4000


async def operator_reply(
    session: AsyncSession,
    *,
    ticket_id: UUID,
    body: str,
    actor_id: UUID,
    ip: str | None = None,
    settings: Settings | None = None,
) -> SupportMessage:
    cleaned = body.strip()
    if not cleaned:
        raise AppError(code="validation_error", message="body must not be empty", status_code=400)
    if len(cleaned) > _MAX_BODY:
        raise AppError(
            code="validation_error",
            message=f"body must be at most {_MAX_BODY} characters",
            status_code=400,
        )

    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None:
        raise AppError(code="not_found", message="Ticket not found", status_code=404)
    if ticket.status == "closed":
        raise AppError(code="ticket_closed", message="Ticket is closed", status_code=400)

    now = datetime.now(UTC)
    message = SupportMessage(
        id=uuid4(),
        ticket_id=ticket.id,
        author="operator",
        body=cleaned,
        created_at=now,
    )
    ticket.updated_at = now
    ticket.last_message_at = now
    ticket.last_human_author = "operator"
    session.add(message)
    notification = await notifications_service.create_support_reply_notification(
        session,
        user_id=ticket.user_id,
        ticket_id=ticket.id,
        body=cleaned,
    )
    await record_audit(
        session,
        actor_id=actor_id,
        action="support.reply",
        entity_type="support_ticket",
        entity_id=str(ticket.id),
        metadata={"message_id": str(message.id)},
        ip=ip,
    )
    await session.flush()
    if settings is not None:
        await notifications_service.maybe_push_notification(
            session,
            settings,
            user_id=ticket.user_id,
            kind=notification.kind,
            title=notification.title,
            body=notification.body,
            target_type="support_ticket",
            target_id=ticket.id,
        )
    await session.commit()
    return message
