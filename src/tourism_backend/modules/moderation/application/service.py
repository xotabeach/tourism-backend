"""Приём жалоб на контент.

Жалоба ничего не скрывает сама по себе: она ставит объект в очередь
модерации. Автоскрытие по числу жалоб — способ устроить травлю чужого
маршрута нажатием кнопки, поэтому решение принимает человек в админке.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.api.errors import AppError
from tourism_backend.modules.content.infrastructure.models import Article, ArticleComment
from tourism_backend.modules.moderation.application.schemas import (
    ContentReportCreateIn,
    ContentReportOut,
)
from tourism_backend.modules.moderation.infrastructure.models import ContentReport
from tourism_backend.modules.places.infrastructure.models import Place
from tourism_backend.modules.routes.infrastructure.models import Route


def _not_found() -> AppError:
    return AppError(
        code="report_target_not_found",
        message="Материал не найден",
        status_code=404,
    )


async def _check_target(
    session: AsyncSession,
    *,
    target_type: str,
    target_id: UUID,
    reporter_user_id: UUID,
) -> None:
    """Объект должен существовать, быть видимым и не принадлежать жалобщику.

    Жалоба на свой же комментарий — либо промах, либо попытка нагрузить
    модерацию; и то и другое не нужно пропускать в очередь.
    """
    if target_type == "article_comment":
        comment = await session.get(ArticleComment, target_id)
        if comment is None or comment.status not in ("published", "pending_review"):
            raise _not_found()
        if comment.author_user_id == reporter_user_id:
            raise AppError(
                code="report_own_content",
                message="Нельзя пожаловаться на свой комментарий",
                status_code=400,
            )
        return
    if target_type == "article":
        article = await session.get(Article, target_id)
        if article is None or article.status != "published":
            raise _not_found()
        if article.author_user_id == reporter_user_id:
            raise AppError(
                code="report_own_content",
                message="Нельзя пожаловаться на свою статью",
                status_code=400,
            )
        return
    if target_type == "route":
        route = await session.get(Route, target_id)
        if route is None:
            raise _not_found()
        if route.owner_user_id is not None and route.owner_user_id == reporter_user_id:
            raise AppError(
                code="report_own_content",
                message="Нельзя пожаловаться на свой маршрут",
                status_code=400,
            )
        return
    place = await session.get(Place, target_id)
    if place is None:
        raise _not_found()


async def create_report(
    session: AsyncSession,
    *,
    reporter_user_id: UUID,
    payload: ContentReportCreateIn,
) -> ContentReportOut:
    try:
        target_id = UUID(payload.target_id)
    except ValueError as error:
        raise _not_found() from error

    await _check_target(
        session,
        target_type=payload.target_type,
        target_id=target_id,
        reporter_user_id=reporter_user_id,
    )

    existing = await session.scalar(
        select(ContentReport).where(
            ContentReport.target_type == payload.target_type,
            ContentReport.target_id == target_id,
            ContentReport.reporter_user_id == reporter_user_id,
        )
    )
    if existing is not None:
        # Повторное нажатие — не ошибка: человек просто не увидел, что жалоба
        # уже ушла. Возвращаем первую и говорим об этом честно.
        return _out(existing, already_reported=True)

    report = ContentReport(
        target_type=payload.target_type,
        target_id=target_id,
        reporter_user_id=reporter_user_id,
        reason=payload.reason,
        comment=payload.comment,
        status="new",
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return _out(report)


def _out(report: ContentReport, *, already_reported: bool = False) -> ContentReportOut:
    return ContentReportOut(
        id=str(report.id),
        target_type=report.target_type,  # type: ignore[arg-type]
        target_id=str(report.target_id),
        reason=report.reason,  # type: ignore[arg-type]
        comment=report.comment,
        status=report.status,  # type: ignore[arg-type]
        created_at=report.created_at,
        already_reported=already_reported,
    )


async def set_report_status(
    session: AsyncSession,
    *,
    report_ids: list[UUID],
    status: str,
    resolved_by_user_id: UUID | None = None,
) -> int:
    """Перевести жалобы в другой статус из админки.

    Возвращает число изменённых. Уже решённые не трогаются повторно —
    иначе «взять в работу» на всём списке откатывало бы разобранные.
    """
    if status not in ("new", "in_review", "resolved", "rejected"):
        raise AppError(
            code="report_status_invalid",
            message="Неизвестный статус жалобы",
            status_code=400,
        )
    if not report_ids:
        return 0
    reports = list(
        (await session.scalars(select(ContentReport).where(ContentReport.id.in_(report_ids)))).all()
    )
    changed = 0
    for report in reports:
        if report.status == status:
            continue
        report.status = status
        if status in ("resolved", "rejected"):
            report.resolved_by_user_id = resolved_by_user_id
        changed += 1
    await session.flush()
    return changed
