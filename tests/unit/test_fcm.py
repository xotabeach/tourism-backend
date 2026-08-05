"""Unit tests for optional FCM sender (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tourism_backend.config import Settings
from tourism_backend.modules.notifications.application.fcm import (
    _load_service_account,
    send_data_message,
)


def test_load_service_account_empty() -> None:
    settings = Settings(
        app_env="test",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
        fcm_service_account_json="",
        fcm_service_account_file=None,
    )
    assert _load_service_account(settings) is None


def test_load_service_account_invalid_json() -> None:
    settings = Settings(
        app_env="test",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
        fcm_service_account_json="{not-json",
    )
    assert _load_service_account(settings) is None


def test_load_service_account_missing_project_id() -> None:
    settings = Settings(
        app_env="test",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
        fcm_service_account_json=json.dumps({"type": "service_account"}),
    )
    assert _load_service_account(settings) is None


def test_load_service_account_from_json_and_file(tmp_path: Path) -> None:
    payload = {"project_id": "crimeatrip-test", "type": "service_account"}
    settings = Settings(
        app_env="test",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
        fcm_service_account_json=json.dumps(payload),
    )
    assert _load_service_account(settings) == payload

    path = tmp_path / "fcm.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    from_file = Settings(
        app_env="test",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
        fcm_service_account_file=str(path),
    )
    assert _load_service_account(from_file) == payload

    missing = Settings(
        app_env="test",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
        fcm_service_account_file=str(tmp_path / "missing.json"),
    )
    assert _load_service_account(missing) is None


@pytest.mark.asyncio
async def test_send_data_message_noop_without_config() -> None:
    settings = Settings(
        app_env="test",
        jwt_signing_key="test-jwt-signing-key-at-least-32-chars!!",
    )
    assert await send_data_message(settings, tokens=[], title="t", body="b", data={}) == 0
    assert (
        await send_data_message(
            settings,
            tokens=["token-abcdefghijklmnopqrstuvwxyz"],
            title="t",
            body="b",
            data={"k": "v"},
        )
        == 0
    )
