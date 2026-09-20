"""Shared helpers: Moscow day and salted hashing."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


def moscow_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(MOSCOW).date()


def salted_hash(salt: str, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(salt.encode())
    for part in parts:
        digest.update(b"\x00")
        digest.update(part.encode())
    return digest.hexdigest()
