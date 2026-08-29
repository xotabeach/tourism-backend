"""Offline event time rules for replayed route execution actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tourism_backend.api.errors import AppError
from tourism_backend.modules.route_execution.application.offline_sync import (
    CLOCK_SKEW_TOLERANCE,
    MAX_OFFLINE_LAG,
    resolve_event_time,
    terminal_conflict_details,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
STARTED_AT = NOW - timedelta(hours=6)


def test_missing_client_time_uses_server_now() -> None:
    resolved = resolve_event_time(None, now=NOW, not_before=STARTED_AT)

    assert resolved.effective == NOW
    assert resolved.reported is None
    assert resolved.clamped is False


def test_offline_completion_keeps_the_reported_moment() -> None:
    occurred_at = NOW - timedelta(hours=3)

    resolved = resolve_event_time(occurred_at, now=NOW, not_before=STARTED_AT)

    assert resolved.effective == occurred_at
    assert resolved.reported == occurred_at
    assert resolved.clamped is False


def test_naive_client_time_is_read_as_utc() -> None:
    resolved = resolve_event_time(
        datetime(2026, 8, 29, 9, 0),
        now=NOW,
        not_before=STARTED_AT,
    )

    assert resolved.effective == NOW - timedelta(hours=3)


def test_time_before_the_run_is_clamped_to_its_start() -> None:
    resolved = resolve_event_time(
        STARTED_AT - timedelta(days=1),
        now=NOW,
        not_before=STARTED_AT,
    )

    assert resolved.effective == STARTED_AT
    assert resolved.reported == STARTED_AT - timedelta(days=1)
    assert resolved.clamped is True


def test_small_clock_skew_is_clamped_to_server_now() -> None:
    resolved = resolve_event_time(
        NOW + CLOCK_SKEW_TOLERANCE - timedelta(seconds=30),
        now=NOW,
        not_before=STARTED_AT,
    )

    assert resolved.effective == NOW
    assert resolved.clamped is True


def test_far_future_time_is_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        resolve_event_time(
            NOW + timedelta(days=2),
            now=NOW,
            not_before=STARTED_AT,
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "route_execution_event_time_invalid"
    assert excinfo.value.details == {"reason": "future", "retryable": False}


def test_expired_queue_entry_is_rejected() -> None:
    with pytest.raises(AppError) as excinfo:
        resolve_event_time(
            NOW - MAX_OFFLINE_LAG - timedelta(hours=1),
            now=NOW,
            not_before=STARTED_AT - MAX_OFFLINE_LAG * 2,
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.details == {"reason": "too_old", "retryable": False}


def test_terminal_conflict_tells_the_client_not_to_retry() -> None:
    assert terminal_conflict_details("cancelled") == {
        "status": "cancelled",
        "retryable": False,
    }
