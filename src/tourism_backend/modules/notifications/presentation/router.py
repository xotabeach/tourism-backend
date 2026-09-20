from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from tourism_backend.api.deps import CurrentUserId, DbSession, RedisClient
from tourism_backend.modules.notifications.application import (
    device_tokens as device_token_service,
)
from tourism_backend.modules.notifications.application import service as notifications_service
from tourism_backend.modules.notifications.application.device_token_schemas import (
    DeviceTokenDeleteIn,
    DeviceTokenIn,
)
from tourism_backend.modules.notifications.application.limits import enforce_delete_limit
from tourism_backend.modules.notifications.application.schemas import (
    NotificationDeleteOut,
    NotificationIdsIn,
    NotificationListOut,
    NotificationOut,
)

router = APIRouter(prefix="/me", tags=["notifications"])


@router.get("/notifications", response_model=NotificationListOut)
async def list_my_notifications(
    session: DbSession,
    user_id: CurrentUserId,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> NotificationListOut:
    return await notifications_service.list_notifications(
        session,
        user_id=user_id,
        limit=limit,
        offset=offset,
    )


@router.post("/notifications/read-all", status_code=status.HTTP_200_OK)
async def mark_all_read(
    session: DbSession,
    user_id: CurrentUserId,
) -> dict[str, int]:
    return await notifications_service.mark_all_notifications_read(
        session,
        user_id=user_id,
    )


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
) -> NotificationOut:
    return await notifications_service.mark_notification_read(
        session,
        user_id=user_id,
        notification_id=notification_id,
    )


@router.delete("/notifications/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(
    notification_id: UUID,
    session: DbSession,
    user_id: CurrentUserId,
    redis: RedisClient,
) -> Response:
    await enforce_delete_limit(redis, user_id=user_id, bulk=False)
    await notifications_service.delete_notification(
        session, user_id=user_id, notification_id=notification_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/notifications/delete", response_model=NotificationDeleteOut)
async def delete_notifications(
    payload: NotificationIdsIn,
    session: DbSession,
    user_id: CurrentUserId,
    redis: RedisClient,
) -> NotificationDeleteOut:
    await enforce_delete_limit(redis, user_id=user_id, bulk=False)
    deleted = await notifications_service.delete_notifications(
        session, user_id=user_id, ids=payload.ids
    )
    return NotificationDeleteOut(deleted=deleted)


@router.delete("/notifications", response_model=NotificationDeleteOut)
async def clear_notifications(
    session: DbSession,
    user_id: CurrentUserId,
    redis: RedisClient,
    scope: Literal["read", "all"],
    before: datetime,
    dry_run: bool = False,
) -> NotificationDeleteOut:
    """Clear read or all notifications no newer than ``before``.

    ``dry_run`` only counts, so the app can say how many will go.
    """

    await enforce_delete_limit(redis, user_id=user_id, bulk=True)
    deleted = await notifications_service.clear_notifications(
        session, user_id=user_id, scope=scope, before=before, dry_run=dry_run
    )
    return NotificationDeleteOut(deleted=deleted)


@router.post("/device-tokens", status_code=status.HTTP_204_NO_CONTENT)
async def register_device_token(
    payload: DeviceTokenIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await device_token_service.upsert_device_token(
        session,
        user_id=user_id,
        payload=payload,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/device-tokens", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_device_token(
    payload: DeviceTokenDeleteIn,
    session: DbSession,
    user_id: CurrentUserId,
) -> Response:
    await device_token_service.delete_device_token(
        session,
        user_id=user_id,
        token=payload.token,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
