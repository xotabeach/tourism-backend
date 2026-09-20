"""Which app version an authenticated user is running (X-App-Version)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert

from tourism_backend.modules.app_stats.application.common import moscow_today
from tourism_backend.modules.app_stats.infrastructure.models import AppVersionDailyUser

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"^(\d{1,4}\.\d{1,4}\.\d{1,4})\+(\d{1,9})$")
_PLATFORMS = frozenset({"android", "ios"})
UNKNOWN_VERSION = "unknown"
_DAY_KEY_TTL_SECONDS = 26 * 3600
_background: set[asyncio.Task[None]] = set()


def parse_app_version(raw: str | None) -> tuple[str, int]:
    """`1.2.3+45` -> ("1.2.3", 45); anything else -> ("unknown", 0)."""
    if raw is None or len(raw) > 32:
        return UNKNOWN_VERSION, 0
    match = _VERSION_RE.fullmatch(raw.strip())
    if match is None:
        return UNKNOWN_VERSION, 0
    return match.group(1), int(match.group(2))


def parse_platform(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    return value if value in _PLATFORMS else UNKNOWN_VERSION


async def _write(session_factory: object, user_id: UUID, key: tuple[str, int, str]) -> None:
    version, build, platform = key
    try:
        async with session_factory() as session:  # type: ignore[operator]
            await session.execute(
                insert(AppVersionDailyUser)
                .values(
                    user_id=user_id,
                    day=moscow_today(),
                    app_version=version,
                    build_number=build,
                    platform=platform,
                )
                .on_conflict_do_nothing()
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - statistics never break a request
        logger.warning("app_version_write_failed", exc_info=True)


async def record_app_version(request: Request, user_id: UUID) -> None:
    """Once per user, day, version and platform. Best-effort: never raises."""
    try:
        redis = getattr(request.app.state, "redis", None)
        session_factory = getattr(request.app.state, "session_factory", None)
        if redis is None or session_factory is None:
            return
        version, build = parse_app_version(request.headers.get("x-app-version"))
        platform = parse_platform(request.headers.get("x-app-platform"))
        day = moscow_today()
        key = f"appver:{user_id}:{day}:{version}+{build}:{platform}"
        first = await redis.set(key, "1", nx=True, ex=_DAY_KEY_TTL_SECONDS)
        if not first:
            return
        task = asyncio.create_task(_write(session_factory, user_id, (version, build, platform)))
        _background.add(task)
        task.add_done_callback(_background.discard)
    except Exception:  # noqa: BLE001
        logger.debug("app_version_record_skipped", exc_info=True)
