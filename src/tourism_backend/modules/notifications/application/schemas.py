from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NotificationKind = Literal[
    "route_review",
    "route_published",
    "route_rejected",
    "review_published",
    "review_rejected",
    "profile_like",
    "achievement_unlocked",
    "support_reply",
    "review_reply",
    "expert_granted",
    "expert_revoked",
]
NotificationTargetType = Literal["route", "user", "achievement", "support_ticket"]


class NotificationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NotificationKind
    title: str
    body: str
    actor_user_id: str | None
    actor_display_name: str | None
    target_type: NotificationTargetType | None
    target_id: str | None
    is_read: bool
    created_at: datetime


class NotificationListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[NotificationOut]
    unread_count: int = Field(ge=0)
