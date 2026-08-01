from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

TicketKind = Literal["chat", "route_error", "app_error"]
MessageAuthor = Literal["user", "assistant", "system", "operator"]


class SupportTicketCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TicketKind
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    route_id: UUID | None = None

    @field_validator("subject", "body")
    @classmethod
    def _trim(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned


class SupportMessageCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def _trim(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned


class SupportMessageOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    author: MessageAuthor
    body: str
    created_at: datetime


class SupportTicketOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: TicketKind
    subject: str
    status: str
    route_id: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[SupportMessageOut] = Field(default_factory=list)


class SupportTicketListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SupportTicketOut]
