"""Retention for notifications: how long an inbox entry is worth keeping.

Read ones go after 90 days, anything after 180. Unread ones are kept longer on
purpose - they may be something the person still has to see - but not forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, or_, select

from tourism_backend.modules.notifications.infrastructure.models import Notification

READ_RETENTION_DAYS = 90
ANY_RETENTION_DAYS = 180
DEFAULT_BATCH_SIZE = 5000
MAX_BATCH_SIZE = 20_000
MAX_BATCHES_PER_RUN = 200


def read_cutoff(*, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=READ_RETENTION_DAYS)


def any_cutoff(*, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=ANY_RETENTION_DAYS)


def expired_notification_ids(
    *,
    read_before: datetime,
    any_before: datetime,
    limit: int,
) -> Select[tuple[UUID]]:
    return (
        select(Notification.id)
        .where(
            or_(
                Notification.created_at < any_before,
                (Notification.is_read.is_(True)) & (Notification.created_at < read_before),
            )
        )
        .order_by(Notification.created_at)
        .limit(limit)
    )
