import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tourism_backend.modules.identity.application.display_name import (
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
    validate_display_name,
)
from tourism_backend.modules.identity.infrastructure.models import PREFERENCE_CATEGORIES

PreferenceDifficulty = Literal["easy", "moderate", "hard"]

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


class OtpStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str = Field(min_length=10, max_length=32)
    display_name: str | None = Field(
        default=None,
        min_length=DISPLAY_NAME_MIN_LENGTH,
        max_length=DISPLAY_NAME_MAX_LENGTH,
    )

    @field_validator("phone")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        phone = normalize_ru_phone(value)
        if not _PHONE_RE.match(phone):
            raise ValueError("phone must match +7XXXXXXXXXX")
        return phone

    @field_validator("display_name")
    @classmethod
    def _trim_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_display_name(value)


class OtpStartOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration_required: bool
    consents_required: bool
    otp_sent: bool


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
    travel_plus_active: bool = False
    travel_plus_plan: str | None = None
    travel_plus_expires_at: str | None = None
    ai_chat_enabled: bool = False
    max_route_points: int = 5
    alternatives_count: int = 1
    advanced_filters_enabled: bool = False
    preferred_categories: list[str] = Field(default_factory=list)
    preferred_difficulty: str | None = None
    travels_with_kids: bool = False
    travels_with_pets: bool = False
    preferences_updated_at: str | None = None


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


class MePreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_categories: list[str] = Field(default_factory=list, max_length=4)
    preferred_difficulty: PreferenceDifficulty | None = None
    travels_with_kids: bool = False
    travels_with_pets: bool = False

    @field_validator("preferred_categories")
    @classmethod
    def _known_categories(cls, value: list[str]) -> list[str]:
        unknown = set(value) - set(PREFERENCE_CATEGORIES)
        if unknown:
            raise ValueError(f"unknown categories: {sorted(unknown)}")
        # Dedupe while keeping the submitted order.
        return list(dict.fromkeys(value))


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
