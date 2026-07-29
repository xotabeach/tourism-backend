from uuid import UUID

from fastapi import APIRouter

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.support.application import service as support_service
from tourism_backend.modules.support.application.schemas import (
    SupportMessageCreateIn,
    SupportMessageOut,
    SupportTicketCreateIn,
    SupportTicketListOut,
    SupportTicketOut,
)

router = APIRouter(prefix="/support", tags=["support"])


@router.post("/tickets", response_model=SupportTicketOut)
async def create_ticket(
    payload: SupportTicketCreateIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> SupportTicketOut:
    return await support_service.create_ticket(session, user_id, payload)


@router.get("/tickets", response_model=SupportTicketListOut)
async def list_tickets(session: DbSession, user_id: CurrentUserId) -> SupportTicketListOut:
    return await support_service.list_tickets(session, user_id)


@router.get("/tickets/{ticket_id}", response_model=SupportTicketOut)
async def get_ticket(
    ticket_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> SupportTicketOut:
    return await support_service.get_ticket(session, user_id, ticket_id)


@router.post("/tickets/{ticket_id}/messages", response_model=SupportMessageOut)
async def add_message(
    ticket_id: UUID,
    payload: SupportMessageCreateIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> SupportMessageOut:
    return await support_service.add_message(session, user_id, ticket_id, payload)
