"""Notification preference fields on /me — DTO bounds and mass-assignment."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tourism_backend.modules.identity.application.schemas import MeOut, MePatchIn


def test_me_patch_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        MePatchIn()


def test_me_patch_accepts_notification_flags_without_display_name() -> None:
    payload = MePatchIn(notify_push_enabled=False, notify_sms_enabled=True)
    assert payload.display_name is None
    assert payload.notify_push_enabled is False
    assert payload.notify_sms_enabled is True


def test_me_patch_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        MePatchIn.model_validate({"notify_push_enabled": True, "role": "admin"})


def test_me_out_includes_notification_defaults() -> None:
    me = MeOut(id="u1", display_name="Никита", phone="+79001234567")
    assert me.notify_push_enabled is True
    assert me.notify_sms_enabled is False
    assert me.notify_haptics_enabled is True


def test_me_patch_rejects_empty_display_name() -> None:
    with pytest.raises(ValidationError):
        MePatchIn(display_name="   ")
