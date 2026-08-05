from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DevicePlatform = Literal["ios", "android"]


class DeviceTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=512)
    platform: DevicePlatform

    @field_validator("token")
    @classmethod
    def _trim_token(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 20:
            raise ValueError("token too short")
        return cleaned


class DeviceTokenDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=512)

    @field_validator("token")
    @classmethod
    def _trim_token(cls, value: str) -> str:
        return value.strip()
