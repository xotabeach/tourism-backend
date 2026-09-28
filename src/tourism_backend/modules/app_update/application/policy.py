"""Whether an installed app build must be updated (BACKEND-5).

The server decides, by its own clock: a build older than the published APK
gets a soft prompt, and after ``hard_after_days`` since that APK was
published, a blocking screen. Everything errs towards letting people in —
no manifest, no APK, an unreadable version or a switched-off block all mean
nobody is blocked, because a person blocked with nothing to download is
stuck.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tourism_backend.modules.runtime_config.infrastructure.models import RuntimeSetting

_logger = logging.getLogger("tourism_backend.app_update")

KEY_PREFIX = "au_"
KEY_HARD_ENABLED = "au_hard_enabled"
KEY_HARD_AFTER_DAYS = "au_hard_after_days"
KEY_MIN_SUPPORTED_BUILD = "au_min_supported_build"
KEY_DOWNLOAD_URL = "au_download_url"
KEY_STORE_URL = "au_store_url_android"
KEY_MESSAGE = "au_message"
ALL_KEYS: tuple[str, ...] = (
    KEY_HARD_ENABLED,
    KEY_HARD_AFTER_DAYS,
    KEY_MIN_SUPPORTED_BUILD,
    KEY_DOWNLOAD_URL,
    KEY_STORE_URL,
    KEY_MESSAGE,
)

# крымтрип.рф/download in punycode: the landing starts the APK download at once.
DEFAULT_DOWNLOAD_URL = "https://xn--h1adgncbn4e.xn--p1ai/download"
DEFAULT_HARD_AFTER_DAYS = 3
HARD_AFTER_DAYS_MAX = 60
MIN_SUPPORTED_BUILD_MAX = 1_000_000_000
MESSAGE_MAX = 300

MANIFEST_NAME = "latest.json"
LATEST_APK_NAME = "crimeatrip-latest.apk"

_CLIENT_VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:\+(\d{1,9}))?$")
_URL_RE = re.compile(r"^https://[^\s<>\"']{3,300}$")


class UpdateKind(StrEnum):
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True, slots=True)
class UpdateSettings:
    hard_enabled: bool = True
    hard_after_days: int = DEFAULT_HARD_AFTER_DAYS
    min_supported_build: int | None = None
    download_url: str = DEFAULT_DOWNLOAD_URL
    store_url: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedRelease:
    version: tuple[int, int, int]
    version_text: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class Decision:
    kind: UpdateKind
    #: When a soft prompt turns into a block; None when it never will.
    hard_at: datetime | None = None


def parse_client_version(raw: str | None) -> tuple[tuple[int, int, int], int | None] | None:
    """``"0.3.0+25"`` gives ``((0, 3, 0), 25)``; anything unexpected gives None."""
    if raw is None:
        return None
    match = _CLIENT_VERSION_RE.fullmatch(raw.strip())
    if match is None:
        return None
    major, minor, patch, build = match.groups()
    return (int(major), int(minor), int(patch)), int(build) if build else None


def validate_setting(key: str, value: str) -> str:
    """Normalize an admin-entered value or raise ``ValueError`` (nothing saved)."""
    text = value.strip()
    if key == KEY_HARD_ENABLED:
        if text not in {"on", "off"}:
            raise ValueError("Жёсткий блок: on или off.")
        return text
    if key == KEY_HARD_AFTER_DAYS:
        if not text.isdigit() or not 0 <= int(text) <= HARD_AFTER_DAYS_MAX:
            raise ValueError(f"Срок до блока: целое число от 0 до {HARD_AFTER_DAYS_MAX} дней.")
        return str(int(text))
    if key == KEY_MIN_SUPPORTED_BUILD:
        if not text.isdigit() or not 1 <= int(text) <= MIN_SUPPORTED_BUILD_MAX:
            raise ValueError("Минимальная сборка: целое положительное число.")
        return str(int(text))
    if key in {KEY_DOWNLOAD_URL, KEY_STORE_URL}:
        if not _URL_RE.fullmatch(text):
            raise ValueError("Ссылка должна начинаться с https:// и не содержать пробелов.")
        return text
    if key == KEY_MESSAGE:
        if not 2 <= len(text) <= MESSAGE_MAX:
            raise ValueError(f"Текст: от 2 до {MESSAGE_MAX} символов.")
        return text
    raise ValueError("Неизвестная настройка.")


def parse_settings(raw: Mapping[str, str]) -> UpdateSettings:
    """Stored strings to settings; a bad stored value falls back to its default."""

    def clean(key: str) -> str | None:
        stored = raw.get(key)
        if stored is None:
            return None
        try:
            return validate_setting(key, stored)
        except ValueError:
            _logger.warning("app_update_invalid_setting", extra={"key": key})
            return None

    hard_enabled = clean(KEY_HARD_ENABLED)
    hard_after = clean(KEY_HARD_AFTER_DAYS)
    min_build = clean(KEY_MIN_SUPPORTED_BUILD)
    return UpdateSettings(
        hard_enabled=hard_enabled != "off",
        hard_after_days=int(hard_after) if hard_after is not None else DEFAULT_HARD_AFTER_DAYS,
        min_supported_build=int(min_build) if min_build is not None else None,
        download_url=clean(KEY_DOWNLOAD_URL) or DEFAULT_DOWNLOAD_URL,
        store_url=clean(KEY_STORE_URL),
        message=clean(KEY_MESSAGE),
    )


async def load_settings(session: AsyncSession) -> UpdateSettings:
    """All keys in one query; a DB hiccup means defaults, never a block."""
    try:
        rows = (
            (
                await session.execute(
                    select(RuntimeSetting.key, RuntimeSetting.value).where(
                        RuntimeSetting.key.like(f"{KEY_PREFIX}%")
                    )
                )
            )
            .tuples()
            .all()
        )
    except Exception:  # noqa: BLE001 - falling back is the whole point
        _logger.warning("app_update_settings_unavailable", exc_info=True)
        return UpdateSettings(hard_enabled=False)
    return parse_settings(dict(rows))


class ReleaseReader:
    """``latest.json`` written by apk-receive.sh, re-read when its mtime changes."""

    def __init__(self, app_dir: Path) -> None:
        self._manifest = app_dir / MANIFEST_NAME
        self._apk = app_dir / LATEST_APK_NAME
        self._mtime: float | None = None
        self._release: PublishedRelease | None = None

    def current(self) -> PublishedRelease | None:
        try:
            mtime = self._manifest.stat().st_mtime
            apk_present = self._apk.is_file() and self._apk.stat().st_size > 0
        except OSError:
            return None
        if not apk_present:
            # Nothing to download: prompting, let alone blocking, would strand people.
            return None
        if mtime != self._mtime:
            self._mtime = mtime
            self._release = self._read()
        return self._release

    def _read(self) -> PublishedRelease | None:
        try:
            data = json.loads(self._manifest.read_text(encoding="utf-8"))
            version_text = str(data["version"])
            parsed = parse_client_version(version_text)
            published_at = datetime.fromisoformat(str(data["published_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            _logger.warning("app_update_manifest_unreadable", exc_info=True)
            return None
        if parsed is None:
            _logger.warning("app_update_manifest_bad_version")
            return None
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        return PublishedRelease(
            version=parsed[0], version_text=version_text, published_at=published_at
        )


def decide(
    *,
    platform: str,
    client_version: str | None,
    release: PublishedRelease | None,
    settings: UpdateSettings,
    now: datetime,
) -> Decision:
    # iOS has no store page yet, so there is nowhere to send an iPhone.
    if platform != "android" or release is None:
        return Decision(UpdateKind.NONE)
    parsed = parse_client_version(client_version)
    if parsed is None:
        return Decision(UpdateKind.NONE)
    version, build = parsed
    if (
        settings.hard_enabled
        and settings.min_supported_build is not None
        and build is not None
        and build < settings.min_supported_build
    ):
        # Manual immediate block, e.g. after an incompatible API change.
        return Decision(UpdateKind.HARD, hard_at=now)
    if version >= release.version:
        return Decision(UpdateKind.NONE)
    if not settings.hard_enabled:
        return Decision(UpdateKind.SOFT)
    hard_at = release.published_at + timedelta(days=settings.hard_after_days)
    if now >= hard_at:
        return Decision(UpdateKind.HARD, hard_at=hard_at)
    return Decision(UpdateKind.SOFT, hard_at=hard_at)
