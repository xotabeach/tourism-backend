"""Схемы жалоб на контент."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tourism_backend.modules.moderation.infrastructure.models import (
    MAX_REPORT_COMMENT_LENGTH,
)

ReportTargetType = Literal["article_comment", "article", "route", "place"]
ReportReason = Literal[
    "spam",
    "abuse",
    "inappropriate",
    "misinformation",
    "copyright",
    "other",
]
ReportStatus = Literal["new", "in_review", "resolved", "rejected"]


class ContentReportCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: ReportTargetType
    target_id: str
    reason: ReportReason
    comment: str | None = Field(default=None, max_length=MAX_REPORT_COMMENT_LENGTH)

    @field_validator("comment")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ContentReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    target_type: ReportTargetType
    target_id: str
    reason: ReportReason
    comment: str | None
    status: ReportStatus
    created_at: datetime

    # Жалоба уже была: сервер не создаёт вторую, а возвращает первую. Клиенту
    # важно отличать «приняли» от «вы уже жаловались».
    already_reported: bool = False
