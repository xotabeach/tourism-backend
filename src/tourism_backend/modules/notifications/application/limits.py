"""Per-person limits on deleting notifications (Redis, fixed one-minute window)."""

from __future__ import annotations

from uuid import UUID

from redis.asyncio import Redis

from tourism_backend.api.errors import AppError

WINDOW_SECONDS = 60
# One tap or one queued batch: generous, a person can clear a lot by hand.
SINGLE_LIMIT_PER_WINDOW = 120
# "Clear read" / "clear all": a few a minute is already far more than a person needs.
BULK_LIMIT_PER_WINDOW = 10


async def enforce_delete_limit(redis: Redis, *, user_id: UUID, bulk: bool) -> None:
    key = f"notifications:delete:{'bulk' if bulk else 'single'}:{user_id}"
    limit = BULK_LIMIT_PER_WINDOW if bulk else SINGLE_LIMIT_PER_WINDOW
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, WINDOW_SECONDS)
    if count > limit:
        raise AppError(
            code="rate_limited",
            message="Too many attempts. Try again later.",
            status_code=429,
        )
