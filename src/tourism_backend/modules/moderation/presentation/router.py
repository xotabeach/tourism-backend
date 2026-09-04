"""HTTP-поверхность жалоб."""

from fastapi import APIRouter, status

from tourism_backend.api.deps import CurrentUserId, DbSession
from tourism_backend.modules.moderation.application import service
from tourism_backend.modules.moderation.application.schemas import (
    ContentReportCreateIn,
    ContentReportOut,
)

router = APIRouter(tags=["moderation"])


@router.post(
    "/reports",
    response_model=ContentReportOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_report(
    session: DbSession,
    reporter_user_id: CurrentUserId,
    payload: ContentReportCreateIn,
) -> ContentReportOut:
    """Пожаловаться на комментарий, статью, маршрут или место.

    Только для вошедших: анонимная жалоба ничем не подкреплена и её нельзя
    ограничить одной на человека.
    """
    return await service.create_report(
        session,
        reporter_user_id=reporter_user_id,
        payload=payload,
    )
