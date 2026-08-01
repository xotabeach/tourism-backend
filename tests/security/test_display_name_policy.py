import pytest
from pydantic import ValidationError

from tourism_backend.modules.identity.application.display_name import (
    DISPLAY_NAME_MAX_LENGTH,
    contains_prohibited_language,
    validate_display_name,
)
from tourism_backend.modules.identity.application.schemas import MePatchIn, OtpRequestIn


def test_validate_display_name_accepts_normal_names() -> None:
    assert validate_display_name("  Никита  ") == "Никита"
    assert validate_display_name("Ada Lovelace"[:DISPLAY_NAME_MAX_LENGTH])


def test_validate_display_name_rejects_overlong() -> None:
    with pytest.raises(ValueError, match="1..20"):
        validate_display_name("x" * (DISPLAY_NAME_MAX_LENGTH + 1))


@pytest.mark.parametrize(
    "value",
    [
        "fuckyou",
        "F u c k",
        "sh1t",
        "сука",
        "БлЯть",
        "хуевый",
    ],
)
def test_contains_prohibited_language(value: str) -> None:
    assert contains_prohibited_language(value)
    with pytest.raises(ValueError, match="prohibited"):
        validate_display_name(value)


def test_otp_and_me_patch_enforce_policy() -> None:
    with pytest.raises(ValidationError):
        OtpRequestIn(display_name="x" * 21, phone="+79001234567")
    with pytest.raises(ValidationError):
        OtpRequestIn(display_name="fuck", phone="+79001234567")
    with pytest.raises(ValidationError):
        MePatchIn(display_name="блять")
    ok = OtpRequestIn(display_name="Никита", phone="+79001234567")
    assert ok.display_name == "Никита"
