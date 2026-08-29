"""Bounded rules for replaying route execution actions recorded offline.

A phone can complete a stop in a mountain valley and reach the network hours
later.  The queued action is still true, so the server accepts a client
timestamp, but a client clock is untrusted input: it can be wrong, replayed or
deliberately shifted.  These helpers keep the stored facts inside a defensible
window instead of writing whatever the device reports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from tourism_backend.api.errors import AppError

EventAction = Literal["complete_stop", "complete", "cancel"]

#: How far a device clock may run ahead of the server before we reject it.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)
#: How long a queued offline action stays acceptable.
MAX_OFFLINE_LAG = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class ResolvedEventTime:
    """Client-reported time plus the value that may enter derived state."""

    effective: datetime
    reported: datetime | None
    clamped: bool


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve_event_time(
    occurred_at: datetime | None,
    *,
    now: datetime,
    not_before: datetime,
) -> ResolvedEventTime:
    """Validate an offline timestamp and return the value we may persist.

    Out-of-window values are rejected rather than silently corrected: a stop
    "completed" next week or a month-old queue entry means the client state is
    no longer trustworthy.  Values inside the window are clamped to the run's
    own timeline so history can never show a stop finished before its start.
    """

    server_now = as_utc(now)
    floor = as_utc(not_before)
    if occurred_at is None:
        return ResolvedEventTime(
            effective=max(server_now, floor),
            reported=None,
            clamped=False,
        )

    reported = as_utc(occurred_at)
    if reported > server_now + CLOCK_SKEW_TOLERANCE:
        raise AppError(
            code="route_execution_event_time_invalid",
            message="Время действия не может быть в будущем",
            status_code=422,
            details={"reason": "future", "retryable": False},
        )
    if reported < server_now - MAX_OFFLINE_LAG:
        raise AppError(
            code="route_execution_event_time_invalid",
            message="Действие слишком старое, чтобы его синхронизировать",
            status_code=422,
            details={"reason": "too_old", "retryable": False},
        )

    effective = min(reported, server_now)
    effective = max(effective, floor)
    return ResolvedEventTime(
        effective=effective,
        reported=reported,
        clamped=effective != reported,
    )


def terminal_conflict_details(status: str) -> dict[str, object]:
    """Tell a replaying client that retrying this action cannot succeed."""

    return {"status": status, "retryable": False}


__all__: Sequence[str] = (
    "CLOCK_SKEW_TOLERANCE",
    "MAX_OFFLINE_LAG",
    "EventAction",
    "ResolvedEventTime",
    "as_utc",
    "resolve_event_time",
    "terminal_conflict_details",
)
