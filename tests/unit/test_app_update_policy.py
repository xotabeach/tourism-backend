"""Outdated build prompt and block (BACKEND-5)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Response

from tourism_backend.modules.app_update.application import policy
from tourism_backend.modules.app_update.application.policy import (
    PublishedRelease,
    ReleaseReader,
    UpdateKind,
    UpdateSettings,
    decide,
    parse_client_version,
    parse_settings,
    validate_setting,
)
from tourism_backend.modules.app_update.presentation import router as router_module

PUBLISHED = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
RELEASE = PublishedRelease(version=(0, 3, 1), version_text="0.3.1", published_at=PUBLISHED)


def _decide(version: str | None, *, now: datetime, **settings: object) -> policy.Decision:
    return decide(
        platform="android",
        client_version=version,
        release=RELEASE,
        settings=UpdateSettings(**settings),  # type: ignore[arg-type]
        now=now,
    )


def test_client_version_parsing() -> None:
    assert parse_client_version("0.3.0+25") == ((0, 3, 0), 25)
    assert parse_client_version("0.3.0") == ((0, 3, 0), None)
    assert parse_client_version("latest") is None
    assert parse_client_version(None) is None


def test_current_and_newer_builds_are_left_alone() -> None:
    later = PUBLISHED + timedelta(days=30)
    assert _decide("0.3.1+26", now=later).kind is UpdateKind.NONE
    assert _decide("0.4.0+30", now=later).kind is UpdateKind.NONE


def test_an_older_build_is_prompted_then_blocked_at_exactly_three_days() -> None:
    boundary = PUBLISHED + timedelta(days=3)
    soft = _decide("0.3.0+25", now=boundary - timedelta(seconds=1))
    assert soft.kind is UpdateKind.SOFT
    assert soft.hard_at == boundary
    assert _decide("0.3.0+25", now=boundary).kind is UpdateKind.HARD


def test_the_kill_switch_turns_every_block_into_a_prompt() -> None:
    later = PUBLISHED + timedelta(days=30)
    decision = _decide("0.2.5+24", now=later, hard_enabled=False, min_supported_build=100)
    assert decision.kind is UpdateKind.SOFT
    assert decision.hard_at is None


def test_min_supported_build_blocks_at_once() -> None:
    right_after = PUBLISHED + timedelta(minutes=5)
    assert _decide("0.3.1+24", now=right_after, min_supported_build=25).kind is UpdateKind.HARD
    assert _decide("0.3.1+25", now=right_after, min_supported_build=25).kind is UpdateKind.NONE


def test_nobody_is_blocked_without_a_release_an_android_or_a_readable_version() -> None:
    later = PUBLISHED + timedelta(days=30)
    settings = UpdateSettings()
    assert (
        decide(
            platform="android", client_version="0.1.0+1", release=None, settings=settings, now=later
        ).kind
        is UpdateKind.NONE
    )
    assert (
        decide(
            platform="ios", client_version="0.1.0+1", release=RELEASE, settings=settings, now=later
        ).kind
        is UpdateKind.NONE
    )
    assert _decide("garbage", now=later).kind is UpdateKind.NONE


def test_settings_validation_and_fallback() -> None:
    assert validate_setting("au_hard_after_days", " 5 ") == "5"
    with pytest.raises(ValueError, match="Срок до блока"):
        validate_setting("au_hard_after_days", "-1")
    with pytest.raises(ValueError, match="https://"):
        validate_setting("au_download_url", "http://example.com/app.apk")
    parsed = parse_settings(
        {"au_hard_after_days": "zero", "au_hard_enabled": "off", "au_store_url_android": "x"}
    )
    assert parsed.hard_after_days == policy.DEFAULT_HARD_AFTER_DAYS
    assert parsed.hard_enabled is False
    assert parsed.store_url is None
    assert parsed.download_url == policy.DEFAULT_DOWNLOAD_URL


def _publish(app_dir: Path, *, version: str = "0.3.1", apk: bool = True) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "latest.json").write_text(
        json.dumps({"version": version, "sha256": "x", "published_at": "2026-09-20T12:00:00Z"}),
        encoding="utf-8",
    )
    if apk:
        (app_dir / "crimeatrip-latest.apk").write_bytes(b"PK\x03\x04")


def test_release_reader_needs_both_manifest_and_apk(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    assert ReleaseReader(app_dir).current() is None
    _publish(app_dir, apk=False)
    assert ReleaseReader(app_dir).current() is None
    _publish(app_dir)
    release = ReleaseReader(app_dir).current()
    assert release is not None
    assert release.version == (0, 3, 1)
    assert release.published_at == PUBLISHED


def test_release_reader_rereads_a_new_manifest(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    _publish(app_dir)
    reader = ReleaseReader(app_dir)
    assert reader.current() is not None
    (app_dir / "latest.json").write_text("{broken", encoding="utf-8")
    os.utime(app_dir / "latest.json", (1, 1))
    assert reader.current() is None


def _request(media_dir: Path, headers: dict[str, str] | None = None) -> MagicMock:
    request = MagicMock()
    request.app.state = SimpleNamespace(media_dir=media_dir)
    request.headers = headers or {}
    return request


async def test_endpoint_reads_the_version_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish(tmp_path / "app")
    monkeypatch.setattr(router_module, "load_settings", AsyncMock(return_value=UpdateSettings()))
    response = Response()
    result = await router_module.read_version_policy(
        _request(tmp_path, {"x-app-version": "0.2.5+24"}),
        response,
        MagicMock(),
        platform="Android",
        version=None,
    )
    assert result.update_kind == "hard"  # published 2026-09-20, long past three days
    assert result.latest_version == "0.3.1"
    assert result.download_url == policy.DEFAULT_DOWNLOAD_URL
    assert "max-age" in response.headers["Cache-Control"]


async def test_endpoint_without_a_release_blocks_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "load_settings", AsyncMock(return_value=UpdateSettings()))
    result = await router_module.read_version_policy(
        _request(tmp_path), Response(), MagicMock(), platform="android", version="0.1.0+1"
    )
    assert result.update_kind == "none"
    assert result.download_url is None
