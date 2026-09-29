"""Public version policy the app checks on start (BACKEND-5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from tourism_backend.api.deps import DbSession
from tourism_backend.modules.app_update.application.policy import (
    ReleaseReader,
    UpdateKind,
    decide,
    load_settings,
)

router = APIRouter(tags=["app-content"])

DEFAULT_MESSAGE = (
    "Вышла новая версия КрымТрипа. Обновите приложение, чтобы пользоваться "
    "новыми функциями и исправлениями."
)
_CACHE_SECONDS = 300


class VersionPolicyOut(BaseModel):
    update_kind: Literal["none", "soft", "hard"]
    latest_version: str | None = None
    published_at: datetime | None = None
    #: When a soft prompt becomes a block, so the dialog can say so.
    hard_at: datetime | None = None
    download_url: str | None = None
    store_url: str | None = None
    message: str = Field(default=DEFAULT_MESSAGE, max_length=300)


def _release_reader(request: Request) -> ReleaseReader:
    reader: ReleaseReader | None = getattr(request.app.state, "release_reader", None)
    if reader is None:
        media_dir = Path(request.app.state.media_dir)
        reader = ReleaseReader(media_dir / "app")
        request.app.state.release_reader = reader
    return reader


@router.get("/app/version-policy", response_model=VersionPolicyOut)
async def read_version_policy(
    request: Request,
    response: Response,
    session: DbSession,
    platform: str = Query(default="android", max_length=16),
    version: str | None = Query(default=None, max_length=32),
) -> VersionPolicyOut:
    """Guest-readable: an outdated build may be signed out, and still must be told."""
    response.headers["Cache-Control"] = f"private, max-age={_CACHE_SECONDS}"
    release = _release_reader(request).current()
    settings = await load_settings(session)
    decision = decide(
        platform=platform.strip().lower(),
        client_version=version or request.headers.get("x-app-version"),
        release=release,
        settings=settings,
        now=datetime.now(UTC),
    )
    if release is None:
        return VersionPolicyOut(update_kind=UpdateKind.NONE.value)
    return VersionPolicyOut(
        update_kind=decision.kind.value,
        latest_version=release.version_text,
        published_at=release.published_at,
        hard_at=decision.hard_at,
        download_url=settings.download_url,
        store_url=settings.store_url,
        message=settings.message or DEFAULT_MESSAGE,
    )
