"""Where the app sends its error reports (BACKEND-73, spec 47)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from tourism_backend.api.deps import CurrentUserId, DbSession, RedisClient
from tourism_backend.api.errors import AppError
from tourism_backend.modules.app_stats.application.error_reports import (
    MAX_BODY_BYTES,
    AppErrorsIn,
    AppErrorsOut,
    record_reports,
)

router = APIRouter(tags=["app-content"])


@router.post("/app/errors", response_model=AppErrorsOut, status_code=202)
async def report_app_errors(
    request: Request,
    payload: AppErrorsIn,
    session: DbSession,
    redis: RedisClient,
    user_id: CurrentUserId,
) -> AppErrorsOut:
    """Signed-in only; up to 20 reports an hour and 64 KB a request."""
    if len(await request.body()) > MAX_BODY_BYTES:
        raise AppError(
            code="payload_too_large", message="Too many reports at once", status_code=413
        )
    return await record_reports(session, redis, user_id=user_id, payload=payload)
