"""Retention for the anti-fraud event log (``route_pace_violations``).

Events older than the cutoff are removed, except those of a run whose points
hold is still open or was closed inside the window: an operator may still need
them to decide or to explain the decision. Holds themselves are kept as the
permanent record of what happened to the points.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, or_, select

from tourism_backend.modules.route_execution.infrastructure.models import (
    RoutePaceViolation,
    RoutePointsHold,
)

DEFAULT_RETENTION_DAYS = 90
MAX_RETENTION_DAYS = 3650
DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 5000
MAX_BATCHES_PER_RUN = 100


def retention_cutoff(days: int, *, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=days)


def eligible_violation_ids(*, cutoff: datetime, limit: int) -> Select[tuple[UUID]]:
    protected = select(RoutePointsHold.execution_id).where(
        or_(RoutePointsHold.status == "held", RoutePointsHold.decided_at >= cutoff)
    )
    return (
        select(RoutePaceViolation.id)
        .where(
            RoutePaceViolation.occurred_at < cutoff,
            RoutePaceViolation.execution_id.not_in(protected),
        )
        .order_by(RoutePaceViolation.occurred_at)
        .limit(limit)
    )
