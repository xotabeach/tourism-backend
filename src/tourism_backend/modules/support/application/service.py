from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.media.infrastructure.models import MediaAttachment
from tourism_backend.modules.support.application.attachments import (
    delete_support_attachment,
    save_support_attachment,
)
from tourism_backend.modules.support.application.schemas import (
    SupportAttachmentOut,
    SupportMessageCreateIn,
    SupportMessageOut,
    SupportTicketCreateIn,
    SupportTicketListOut,
    SupportTicketOut,
)
from tourism_backend.modules.support.infrastructure.models import SupportMessage, SupportTicket

_ASSISTANT_WELCOME = (
    "Здравствуйте! Спасибо за обращение. Мы ответим в течение 1–2 рабочих дней. "
    "Пока можете уточнить детали в этом чате."
)

_HUMAN_AUTHORS = frozenset({"user", "operator"})
_MAX_TICKET_ATTACHMENTS = 3


def _touch_ticket_on_message(
    ticket: SupportTicket,
    *,
    author: str,
    at: datetime,
) -> None:
    ticket.updated_at = at
    ticket.last_message_at = at
    if author in _HUMAN_AUTHORS:
        ticket.last_human_author = author


def _message_out(message: SupportMessage) -> SupportMessageOut:
    return SupportMessageOut(
        id=str(message.id),
        author=message.author,  # type: ignore[arg-type]
        body=message.body,
        created_at=message.created_at,
    )


def _attachment_out(attachment: MediaAttachment) -> SupportAttachmentOut:
    return SupportAttachmentOut(
        id=str(attachment.id),
        url=attachment.public_path,
        width=attachment.width,
        height=attachment.height,
    )


async def _load_attachments(session: AsyncSession, ticket_id: UUID) -> list[SupportAttachmentOut]:
    rows = (
        await session.scalars(
            select(MediaAttachment)
            .where(
                MediaAttachment.entity_type == "support_ticket",
                MediaAttachment.entity_id == ticket_id,
                MediaAttachment.role == "gallery",
                MediaAttachment.status == "active",
            )
            .order_by(MediaAttachment.sort_order, MediaAttachment.created_at)
        )
    ).all()
    return [_attachment_out(row) for row in rows]


def _ticket_out(
    ticket: SupportTicket,
    messages: list[SupportMessage],
    attachments: list[SupportAttachmentOut] | None = None,
) -> SupportTicketOut:
    return SupportTicketOut(
        id=str(ticket.id),
        kind=ticket.kind,  # type: ignore[arg-type]
        subject=ticket.subject,
        status=ticket.status,
        route_id=str(ticket.route_id) if ticket.route_id else None,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        messages=[_message_out(m) for m in messages],
        attachments=attachments or [],
    )


async def create_ticket(
    session: AsyncSession,
    user_id: UUID,
    payload: SupportTicketCreateIn,
) -> SupportTicketOut:
    if payload.kind != "route_error" and payload.route_id is not None:
        raise AppError(
            code="validation_error",
            message="route_id is only allowed for route_error tickets",
            status_code=400,
        )

    now = datetime.now(UTC)
    ticket = SupportTicket(
        id=uuid4(),
        user_id=user_id,
        kind=payload.kind,
        subject=payload.subject,
        status="open",
        route_id=payload.route_id,
        created_at=now,
        updated_at=now,
        last_human_author="user",
        last_message_at=now,
    )
    session.add(ticket)
    await session.flush()

    user_msg = SupportMessage(
        id=uuid4(),
        ticket_id=ticket.id,
        author="user",
        body=payload.body,
        created_at=now,
    )
    session.add(user_msg)

    messages = [user_msg]
    if payload.kind == "chat":
        assistant = SupportMessage(
            id=uuid4(),
            ticket_id=ticket.id,
            author="assistant",
            body=_ASSISTANT_WELCOME,
            created_at=now,
        )
        session.add(assistant)
        messages.append(assistant)
        # Welcome is not a human reply — keep awaiting operator.
        _touch_ticket_on_message(ticket, author="assistant", at=now)

    await session.commit()
    return _ticket_out(ticket, messages)


async def list_tickets(session: AsyncSession, user_id: UUID) -> SupportTicketListOut:
    result = await session.execute(
        select(SupportTicket)
        .where(SupportTicket.user_id == user_id)
        .order_by(SupportTicket.created_at.desc())
        .limit(50)
    )
    tickets = list(result.scalars().all())
    items: list[SupportTicketOut] = []
    for ticket in tickets:
        msg_result = await session.execute(
            select(SupportMessage)
            .where(SupportMessage.ticket_id == ticket.id)
            .order_by(SupportMessage.created_at.asc())
            .limit(200)
        )
        messages = list(msg_result.scalars().all())
        attachments = await _load_attachments(session, ticket.id)
        items.append(_ticket_out(ticket, messages, attachments))
    return SupportTicketListOut(items=items)


async def get_ticket(
    session: AsyncSession,
    user_id: UUID,
    ticket_id: UUID,
) -> SupportTicketOut:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None or ticket.user_id != user_id:
        raise AppError(code="not_found", message="Ticket not found", status_code=404)

    msg_result = await session.execute(
        select(SupportMessage)
        .where(SupportMessage.ticket_id == ticket.id)
        .order_by(SupportMessage.created_at.asc())
        .limit(200)
    )
    messages = list(msg_result.scalars().all())
    attachments = await _load_attachments(session, ticket.id)
    return _ticket_out(ticket, messages, attachments)


async def add_attachment(
    session: AsyncSession,
    user_id: UUID,
    ticket_id: UUID,
    upload: UploadFile,
) -> SupportAttachmentOut:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None or ticket.user_id != user_id:
        raise AppError(code="not_found", message="Ticket not found", status_code=404)

    count = int(
        await session.scalar(
            select(func.count())
            .select_from(MediaAttachment)
            .where(
                MediaAttachment.entity_type == "support_ticket",
                MediaAttachment.entity_id == ticket_id,
                MediaAttachment.role == "gallery",
                MediaAttachment.status == "active",
            )
        )
        or 0
    )
    if count >= _MAX_TICKET_ATTACHMENTS:
        raise AppError(
            code="support_attachment_limit",
            message=f"К обращению можно прикрепить не больше {_MAX_TICKET_ATTACHMENTS} фото",
            status_code=409,
        )

    saved = await save_support_attachment(upload, ticket_id=ticket_id)
    now = datetime.now(UTC)
    attachment = MediaAttachment(
        id=uuid4(),
        entity_type="support_ticket",
        entity_id=ticket_id,
        role="gallery",
        storage_key=saved.storage_key,
        public_path=saved.public_path,
        content_type=saved.content_type,
        byte_size=saved.byte_size,
        width=saved.width,
        height=saved.height,
        checksum_sha256=saved.checksum_sha256,
        status="active",
        uploaded_by_user_id=user_id,
        sort_order=count,
        created_at=now,
        updated_at=now,
    )
    session.add(attachment)
    try:
        await session.commit()
    except Exception:
        delete_support_attachment(saved.storage_key, ticket_id=ticket_id)
        raise
    return _attachment_out(attachment)


async def add_message(
    session: AsyncSession,
    user_id: UUID,
    ticket_id: UUID,
    payload: SupportMessageCreateIn,
) -> SupportMessageOut:
    ticket = await session.get(SupportTicket, ticket_id)
    if ticket is None or ticket.user_id != user_id:
        raise AppError(code="not_found", message="Ticket not found", status_code=404)
    if ticket.status == "closed":
        raise AppError(code="ticket_closed", message="Ticket is closed", status_code=400)

    now = datetime.now(UTC)
    message = SupportMessage(
        id=uuid4(),
        ticket_id=ticket.id,
        author="user",
        body=payload.body,
        created_at=now,
    )
    _touch_ticket_on_message(ticket, author="user", at=now)
    session.add(message)
    await session.commit()
    return _message_out(message)
