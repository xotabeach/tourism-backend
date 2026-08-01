import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tourism_backend.modules.identity.application.display_name import (
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
    validate_display_name,
)

_PHONE_RE = re.compile(r"^\+7\d{10}$")


def normalize_ru_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value.strip())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+7{digits}"
    raise ValueError("phone must be a Russian mobile number")


class OtpRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(
        min_length=DISPLAY_NAME_MIN_LENGTH,
        max_length=DISPLAY_NAME_MAX_LENGTH,
    )
    phone: str = Field(min_length=10, max_length=32)

    @field_validator("display_name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        return validate_display_name(value)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        phone = normalize_ru_phone(value)
        if not _PHONE_RE.match(phone):
            raise ValueError("phone must match +7XXXXXXXXXX")
        return phone


class OtpVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=10, max_length=32)
    code: str = Field(min_length=4, max_length=4)
    privacy_accepted: bool
    personal_data_accepted: bool
    device_label: str | None = Field(default=None, max_length=120)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        phone = normalize_ru_phone(value)
        if not _PHONE_RE.match(phone):
            raise ValueError("phone must match +7XXXXXXXXXX")
        return phone

    @field_validator("code")
    @classmethod
    def _digits_only(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("code must be 4 digits")
        return value


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=20, max_length=256)


class LogoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=20, max_length=256)


class TokenPairOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class MeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    phone: str
    avatar_url: str | None = None
    cover_url: str | None = None
    notify_push_enabled: bool = True
    notify_sms_enabled: bool = False
    notify_haptics_enabled: bool = True


class MePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(
        default=None,
        min_length=DISPLAY_NAME_MIN_LENGTH,
        max_length=DISPLAY_NAME_MAX_LENGTH,
    )
    notify_push_enabled: bool | None = None
    notify_sms_enabled: bool | None = None
    notify_haptics_enabled: bool | None = None

    @field_validator("display_name")
    @classmethod
    def _trim_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_display_name(value)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "MePatchIn":
        if (
            self.display_name is None
            and self.notify_push_enabled is None
            and self.notify_sms_enabled is None
            and self.notify_haptics_enabled is None
        ):
            raise ValueError("at least one field is required")
        return self


class PhoneChangeRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=10, max_length=32)

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        phone = normalize_ru_phone(value)
        if not _PHONE_RE.match(phone):
            raise ValueError("phone must match +7XXXXXXXXXX")
        return phone


class PhoneChangeVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=10, max_length=32)
    code: str = Field(min_length=4, max_length=4)
    privacy_accepted: bool
    personal_data_accepted: bool

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        phone = normalize_ru_phone(value)
        if not _PHONE_RE.match(phone):
            raise ValueError("phone must match +7XXXXXXXXXX")
        return phone

    @field_validator("code")
    @classmethod
    def _digits_only(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("code must be 4 digits")
        return value
