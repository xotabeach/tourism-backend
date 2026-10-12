from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tourism_backend.modules.moderation.infrastructure.models import (
    MAX_REPORT_COMMENT_LENGTH,
    REPORT_REASONS,
)

GroupStatus = Literal["gathering", "started", "settled", "cancelled"]
MemberState = Literal[
    "invited", "requested", "joined", "declined", "revoked", "rejected", "removed", "left"
]
ReportReason = Literal["spam", "abuse", "inappropriate", "misinformation", "copyright", "other"]
assert set(ReportReason.__args__) == set(REPORT_REASONS)  # type: ignore[attr-defined]


class CompanionGroupCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str


class CompanionInviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str


class CompanionJoinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=4, max_length=12)


class CompanionReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: ReportReason
    comment: str | None = Field(default=None, max_length=MAX_REPORT_COMMENT_LENGTH)


class CompanionMemberOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    display_name: str
    avatar_url: str | None = None
    state: MemberState
    is_organizer: bool = False
    ready: bool = False
    joined_late: bool = False
    #: Pending invitations only: when the answer is no longer accepted.
    invite_expires_at: datetime | None = None


class CompanionGroupOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    route_id: str
    route_name: str
    route_cover_url: str | None = None
    status: GroupStatus
    organizer_user_id: str
    organizer_display_name: str
    is_organizer: bool
    max_members: int
    #: The viewer's own row, or None for the organizer before anyone else.
    my_state: MemberState | None
    #: Shown to the people in the group, never to someone only invited.
    join_code: str | None = None
    #: The whole roster for those in the group; the organizer only for an
    #: invitee who has not answered yet.
    members: list[CompanionMemberOut] = Field(default_factory=list)
    joined_count: int = 0
    created_at: datetime
    started_at: datetime | None = None
    settled_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None


class CompanionGroupListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CompanionGroupOut]
