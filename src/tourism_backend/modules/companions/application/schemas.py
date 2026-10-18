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


class CompanionReadyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool


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
    #: The hat on the member's character (BACKEND-65), its build and
    #: clothes (BACKEND-71).
    bean_hat: str | None = None
    bean_kind: str | None = None
    bean_outfit: str | None = None
    #: The member's rank, as on the profile (BACKEND-74).
    rank_title: str | None = None
    #: The member's own run is attached to the group (BACKEND-60); after
    #: the start a member without one is «не стартовал».
    started: bool = False
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


ProgressStatus = Literal["not_started", "walking", "paused", "finished", "quit", "not_finished"]


class CompanionStopMarkOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int
    marked_at: datetime | None = None
    skipped: bool = False
    #: The mark reached the server long after it was made (D11): shown
    #: «без сети», and it takes no first place.
    offline: bool = False


class CompanionMemberProgressOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    display_name: str
    bean_hat: str | None = None
    bean_kind: str | None = None
    bean_outfit: str | None = None
    #: The member's rank, as on the profile (BACKEND-74).
    rank_title: str | None = None
    avatar_url: str | None = None
    is_organizer: bool = False
    joined_late: bool = False
    status: ProgressStatus
    #: The last stop marked or skipped, 0 before the first.
    last_position: int = 0
    marks: list[CompanionStopMarkOut] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: The run went «в зачёт» (spec 15 D2): this member reached the end.
    reached: bool = False


class CompanionStopOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int
    place_name: str
    is_optional: bool = False
    #: Along the route from the first stop, by its legs (BACKEND-74);
    #: None while a leg's length is unknown.
    distance_from_start_meters: int | None = None
    #: Who got here first, among marks made online; None on a mountain route
    #: where the table is hidden, or when nobody has been here yet.
    first_user_id: str | None = None


class CompanionSummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settled: bool
    started_count: int
    reached_count: int
    everyone_reached: bool
    #: Paid kilometres of the farthest walker who reached the end (D14).
    distance_meters: int | None = None
    #: From the group's start to the last arrival.
    total_seconds: int | None = None
    #: First at the finish: reached it without skipping a required stop.
    first_finisher_user_id: str | None = None


class CompanionProgressOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str
    status: GroupStatus
    started_at: datetime | None = None
    #: The «кто первым» table is hidden on mountain routes (D7).
    leaderboard_hidden: bool = False
    stops: list[CompanionStopOut]
    members: list[CompanionMemberProgressOut]
    summary: CompanionSummaryOut
