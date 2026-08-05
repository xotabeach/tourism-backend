"""Device token DTO validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tourism_backend.modules.notifications.application.device_token_schemas import (
    DeviceTokenDeleteIn,
    DeviceTokenIn,
)


def test_device_token_in_trims_and_accepts() -> None:
    payload = DeviceTokenIn(
        token="  " + ("a" * 40) + "  ",
        platform="android",
    )
    assert payload.token == "a" * 40
    assert payload.platform == "android"


def test_device_token_in_rejects_short_and_unknown_platform() -> None:
    with pytest.raises(ValidationError):
        DeviceTokenIn(token="short", platform="android")
    with pytest.raises(ValidationError):
        DeviceTokenIn(token="a" * 40, platform="web")  # type: ignore[arg-type]
    # Long enough for Field min_length, but short after strip.
    with pytest.raises(ValidationError):
        DeviceTokenIn(token=("a" * 5) + (" " * 20), platform="ios")


def test_device_token_delete_trims() -> None:
    payload = DeviceTokenDeleteIn(token="  " + ("b" * 32) + " ")
    assert payload.token == "b" * 32
