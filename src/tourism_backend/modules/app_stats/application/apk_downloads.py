"""Counting APK downloads without ever getting in the way of one.

A pure ASGI middleware: it looks at ``GET /media/app/*.apk``, waits for the
response to start with 200/206 and then counts in a background task. Nothing
here can fail a download - every error is logged and swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import insert

from tourism_backend.modules.app_stats.application.common import moscow_today, salted_hash
from tourism_backend.modules.app_stats.infrastructure.models import ApkDownloadDaily

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

APK_PREFIX = "/media/app/"
DEDUPE_TTL_SECONDS = 30 * 60
HOURLY_LIMIT = 5
_HOUR_SECONDS = 3600
_NAME_VERSION_RE = re.compile(r"^crimeatrip-(\d+\.\d+\.\d+(?:\+\d+)?)\.apk$")
_MANIFEST_VERSION_RE = re.compile(r"^[\w.+-]{1,32}$")
_BOT_RE = re.compile(
    r"bot\b|bot/|crawl|spider|slurp|facebookexternalhit|whatsapp|skypeuripreview|"
    r"vkshare|preview|monitor|uptime|headless|python-requests|curl/|wget/|"
    r"go-http-client|link.?check",
    re.IGNORECASE,
)
_background: set[asyncio.Task[None]] = set()


def is_bot(user_agent: str) -> bool:
    return not user_agent.strip() or _BOT_RE.search(user_agent) is not None


class ManifestReader:
    """Reads ``latest.json`` written by apk-receive.sh; cached by mtime."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime: float | None = None
        self._version = "latest"

    def latest_version(self) -> str:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return "latest"
        if mtime != self._mtime:
            self._mtime = mtime
            self._version = "latest"
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8")).get("version")
                if isinstance(raw, str) and _MANIFEST_VERSION_RE.fullmatch(raw):
                    self._version = raw
            except (OSError, ValueError, AttributeError):
                logger.warning("apk_manifest_unreadable", exc_info=True)
        return self._version


def apk_version_for(path: str, manifest: ManifestReader) -> str:
    name = path.rsplit("/", 1)[-1]
    if name == "crimeatrip-latest.apk":
        return manifest.latest_version()
    match = _NAME_VERSION_RE.fullmatch(name)
    return match.group(1) if match else "other"


async def _count(
    *,
    redis: Any,
    session_factory: Any,
    salt: str,
    ip: str,
    user_agent: str,
    source: str,
    apk_version: str,
) -> None:
    try:
        if is_bot(user_agent):
            return
        if redis is not None:
            try:
                fresh = await redis.set(
                    f"apkdl:dedupe:{salted_hash(salt, ip, user_agent, apk_version)}",
                    "1",
                    nx=True,
                    ex=DEDUPE_TTL_SECONDS,
                )
                if not fresh:
                    return
                hour_key = f"apkdl:hour:{salted_hash(salt, ip)}"
                hits = int(await redis.incr(hour_key))
                if hits == 1:
                    await redis.expire(hour_key, _HOUR_SECONDS)
                if hits > HOURLY_LIMIT:
                    return
            except Exception:  # noqa: BLE001 - without Redis we count without dedupe
                logger.warning("apk_stats_redis_failed", exc_info=True)
        async with session_factory() as session:
            await session.execute(
                insert(ApkDownloadDaily)
                .values(day=moscow_today(), source=source, apk_version=apk_version, count=1)
                .on_conflict_do_update(
                    index_elements=["day", "source", "apk_version"],
                    set_={"count": ApkDownloadDaily.count + 1},
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("apk_stats_count_failed", exc_info=True)


class ApkDownloadCounterMiddleware:
    def __init__(self, app: ASGIApp, *, media_dir: Path) -> None:
        self.app = app
        self._manifest = ManifestReader(media_dir / "app" / "latest.json")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "GET"
            or not scope["path"].startswith(APK_PREFIX)
            or not scope["path"].endswith(".apk")
        ):
            await self.app(scope, receive, send)
            return
        counted = False

        async def counting_send(message: Message) -> None:
            nonlocal counted
            if message["type"] == "http.response.start" and not counted:
                counted = True
                if message["status"] in (200, 206):
                    self._schedule(scope)
            await send(message)

        await self.app(scope, receive, counting_send)

    def _schedule(self, scope: Scope) -> None:
        try:
            app = scope["app"]
            settings = app.state.settings
            if not settings.apk_stats_enabled:
                return
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]
            }
            client = scope.get("client")
            task = asyncio.create_task(
                _count(
                    redis=getattr(app.state, "redis", None),
                    session_factory=app.state.session_factory,
                    salt=settings.apk_stats_salt,
                    ip=client[0] if client else "",
                    user_agent=headers.get("user-agent", ""),
                    source="landing" if headers.get("x-download-source") == "landing" else "direct",
                    apk_version=apk_version_for(scope["path"], self._manifest),
                )
            )
            _background.add(task)
            task.add_done_callback(_background.discard)
        except Exception:  # noqa: BLE001
            logger.warning("apk_stats_schedule_failed", exc_info=True)
