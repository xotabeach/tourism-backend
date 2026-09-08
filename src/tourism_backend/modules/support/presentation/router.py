from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Query, Response, UploadFile

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.support.application import service as support_service
from tourism_backend.modules.support.application.help_search import (
    HelpArticleOut,
    HelpSearchIn,
    HelpSearchOut,
    read_help,
    search_help,
)
from tourism_backend.modules.support.application.schemas import (
    SupportAttachmentOut,
    SupportMessageCreateIn,
    SupportMessageOut,
    SupportTicketCreateIn,
    SupportTicketListOut,
    SupportTicketOut,
)

router = APIRouter(prefix="/support", tags=["support"])

AppVersionQuery = Annotated[str, Query(pattern=r"^\d+\.\d+\.\d+$", max_length=32)]


@router.post("/help/search", response_model=HelpSearchOut)
async def help_search(
    payload: HelpSearchIn,
    session: DbSession,
    response: Response,
) -> HelpSearchOut:
    # Published generic instructions contain no private ticket/account data.
    # Public reading also allows help with login; ticket APIs remain owner-bound.
    response.headers["Cache-Control"] = "no-store"
    # Free-form support questions belong in a body, not proxy/access-log URLs.
    return await search_help(session, query=payload.q, app_version=payload.app_version)


@router.get("/help/{article_id}", response_model=HelpArticleOut)
async def help_article(
    article_id: str,
    session: DbSession,
    response: Response,
    app_version: AppVersionQuery,
    revision: Annotated[int, Query(ge=1)],
) -> HelpArticleOut:
    response.headers["Cache-Control"] = "no-store"
    return await read_help(
        session, article_id=article_id, revision=revision, app_version=app_version
    )


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


@router.post("/tickets/{ticket_id}/attachments", response_model=SupportAttachmentOut)
async def add_attachment(
    ticket_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
) -> SupportAttachmentOut:
    return await support_service.add_attachment(session, user_id, ticket_id, file)


@router.post("/tickets/{ticket_id}/messages", response_model=SupportMessageOut)
async def add_message(
    ticket_id: UUID,
    payload: SupportMessageCreateIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> SupportMessageOut:
    return await support_service.add_message(session, user_id, ticket_id, payload)
