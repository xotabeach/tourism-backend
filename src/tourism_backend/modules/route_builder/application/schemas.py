"""Schemas for algorithmic / AI catalog match (Phase 8A first slice)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

from tourism_backend.modules.route_builder.application.dialogue import DialogueGoal
from tourism_backend.modules.route_builder.application.itinerary import TripPlanOut
from tourism_backend.modules.routes.application.schemas import (
    RouteGeometryOut,
    RouteListItemOut,
    RouteStopOut,
)

# "photo" — «инстаграм-маршрут»: точки ради видов и кадров, а не ради
# самого пути. Отдельный тип, а не интерес «Фото»: он меняет и подбор
# точек, и порядок обхода (закат — в конце).
TripType = Literal["romance", "rest", "adventure", "active", "photo"]
DurationOption = Literal["d1_2", "d3_5", "d6_7", "d7plus"]
PaceOption = Literal["calm", "moderate", "active"]
TransportMode = Literal["walk", "car", "public", "mixed"]
MatchStrategy = Literal["algorithmic", "ai_catalog_rank"]
DayKind = Literal["any", "weekday", "weekend"]


class RouteMatchParamsIn(BaseModel):
    """Normalized form params + optional advanced fields for Travel+/future UI."""

    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1, max_length=80)
    search_area: str | None = Field(default=None, min_length=1, max_length=80)
    preferred_localities: list[str] = Field(default_factory=list, max_length=8)
    flexible_start: bool = False
    planning_mode: Literal["discover", "custom"] = "discover"
    dialogue_goal: DialogueGoal = "clarify"
    trip_type: TripType | None = None
    duration: DurationOption = "d3_5"
    people: int = Field(default=2, ge=1, le=20)
    interests: list[str] = Field(default_factory=list, max_length=12)
    pace: PaceOption = "calm"

    # Advanced / future — accepted now, scored when present
    season: str | None = Field(default=None, max_length=32)
    transport_mode: TransportMode | None = None
    day_kind: DayKind = "any"
    budget_amount: int | None = Field(default=None, ge=0, le=1_000_000)
    paid_ok: bool | None = None
    with_children: bool | None = None
    with_pets: bool | None = None
    avoid_crowds: bool | None = None
    trip_start_date: date | None = None
    region_slug: str = Field(default="crimea", min_length=1, max_length=128)

    @field_validator("city", "season", "region_slug", "search_area")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Value must not be blank")
        return cleaned

    @field_validator("interests", "preferred_localities")
    @classmethod
    def clean_interests(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            item = raw.strip()
            if not item:
                continue
            if len(item) > 40:
                raise ValueError("Interest is too long")
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(item)
        if len(cleaned) > 12:
            raise ValueError("Too many interests")
        return cleaned


class RouteMatchHitOut(BaseModel):
    route: RouteListItemOut
    score: float = Field(ge=0.0, le=1.0)
    band: Literal["ideal", "close"]
    reasons: list[str] = Field(default_factory=list, max_length=8)


GenerateChannel = Literal["form", "chat"]
ProposalStatus = Literal["draft", "accepted", "rejected", "superseded"]


class QuotaSnapshotOut(BaseModel):
    daily_used: int = Field(ge=0)
    weekly_used: int = Field(ge=0)
    daily_remaining: int | None = None
    weekly_remaining: int | None = None


class RouteMatchOut(BaseModel):
    strategy: MatchStrategy
    ideal: list[RouteMatchHitOut]
    close: list[RouteMatchHitOut]
    offer_generate: bool
    ai_rerank_eligible: bool = False
    ai_rerank_applied: bool = False
    scored_total: int = Field(ge=0)
    params_echo: RouteMatchParamsIn
    quota: QuotaSnapshotOut | None = None


class RouteGenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: GenerateChannel = "form"
    params: RouteMatchParamsIn


class PlaceChipBlockOut(BaseModel):
    type: Literal["place_chip"] = "place_chip"
    place_id: str
    title: str
    subtitle: str | None = None
    image_url: str | None = None
    duration_minutes: int | None = None


class ProposalLocationOut(BaseModel):
    """One stop in the assembled-route preview (design-spec screen 3)."""

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    subtitle: str | None = Field(default=None, max_length=80)
    index: int = Field(default=1, ge=1, le=22)


class RouteProposalCardBlockOut(BaseModel):
    type: Literal["route_proposal_card"] = "route_proposal_card"
    proposal_id: str
    title: str
    stops_count: int
    duration_minutes: int
    cover_url: str | None = None
    place_ids: list[str]
    # Design-spec extras for the chat route preview (screen 2/3 of
    # design-spec-travel-agent.md). All optional; mobile renders what it gets.
    rating: float | None = Field(default=None, ge=0, le=5)
    distance_km: float | None = Field(default=None, ge=0)
    locality_label: str | None = Field(default=None, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=8)
    budget_label: str | None = Field(default=None, max_length=40)
    budget_caption: str = Field(default="Минимальный бюджет", max_length=40)
    difficulty_label: str | None = Field(default=None, max_length=40)
    primary_action_label: str = Field(default="Пройти маршрут", max_length=40)
    # ``catalog`` = existing DB route preview; ``assembled`` = generated detail.
    card_variant: Literal["catalog", "assembled", "compact"] = "compact"
    gallery_urls: list[str] = Field(default_factory=list, max_length=8)
    start_label: str | None = Field(default=None, max_length=120)
    start_subtitle: str | None = Field(default=None, max_length=120)
    finish_label: str | None = Field(default=None, max_length=120)
    finish_subtitle: str | None = Field(default=None, max_length=120)
    locations: list[ProposalLocationOut] = Field(default_factory=list, max_length=22)
    route_id: str | None = Field(default=None, max_length=64)


class CatalogRouteItemOut(BaseModel):
    """One editorial/catalog hit inside a chat carousel (design-spec screen 2)."""

    route_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    cover_url: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    distance_km: float | None = Field(default=None, ge=0)
    locality_label: str | None = Field(default=None, max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=8)
    budget_label: str | None = Field(default=None, max_length=40)
    difficulty_label: str | None = Field(default=None, max_length=40)
    stops_count: int = 0
    duration_minutes: int = 0


class CatalogMatchBlockOut(BaseModel):
    """Carousel of existing catalog routes before custom generate."""

    type: Literal["catalog_match"] = "catalog_match"
    routes: list[CatalogRouteItemOut] = Field(default_factory=list, max_length=5)


class ActionsBlockOut(BaseModel):
    type: Literal["actions"] = "actions"
    actions: list[dict[str, str]]
    # ``stack`` = full-width outline rows (design-spec); ``wrap`` = chips;
    # ``sheet`` = single trigger button opening a modal picker list.
    layout: Literal["wrap", "stack", "sheet"] = "wrap"
    # Title shown at the top of the modal picker when layout == "sheet".
    sheet_title: str | None = None


class SliderBlockOut(BaseModel):
    """Numeric slider control rendered under an assistant message."""

    type: Literal["slider"] = "slider"
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    min_value: float = 0
    max_value: float = 1
    step: float = 1
    value: float | None = None
    unit: str | None = Field(default=None, max_length=16)


class ToggleBlockOut(BaseModel):
    """Boolean control rendered under an assistant message."""

    type: Literal["toggle"] = "toggle"
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    value: bool = False


class SelectOptionOut(BaseModel):
    """One option in a :class:`SelectBlockOut`."""

    value: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)


class SelectBlockOut(BaseModel):
    """Dropdown rendered inside the assistant bubble.

    Deliberately generic rather than a "city picker": the agent supplies the
    options and the client returns the chosen ``value`` through the same path
    as slider and toggle values, knowing nothing about the field's meaning.
    The start city is simply the first question asked this way.
    """

    type: Literal["select"] = "select"
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    options: list[SelectOptionOut] = Field(min_length=1, max_length=40)
    value: str | None = Field(default=None, max_length=120)
    placeholder: str = Field(default="Выберите вариант", max_length=60)


class RecommendationCardBlockOut(BaseModel):
    """Seasonal / editorial tip the user can accept with one tap."""

    type: Literal["recommendation_card"] = "recommendation_card"
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=500)
    accept_action_id: str = Field(min_length=1, max_length=64)
    accept_label: str = Field(default="Попробуем так", max_length=80)


ChatBlockOut = (
    PlaceChipBlockOut
    | RouteProposalCardBlockOut
    | CatalogMatchBlockOut
    | ActionsBlockOut
    | SliderBlockOut
    | ToggleBlockOut
    | SelectBlockOut
    | RecommendationCardBlockOut
)


class RouteProposalPreviewOut(BaseModel):
    proposal_id: str
    title: str
    stops: list[RouteStopOut]
    geometry: RouteGeometryOut | None = None
    distance_meters: int = 0
    synthetic: bool = True
    static_map_url: str | None = None
    trip_plan: TripPlanOut | None = None


class ProposalTripDateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date


class RouteProposalOut(BaseModel):
    proposal_id: str
    status: ProposalStatus
    channel: GenerateChannel
    title: str
    assistant_text: str
    place_ids: list[str]
    duration_minutes: int
    cover_url: str | None = None
    route_id: str | None = None
    blocks: list[ChatBlockOut]
    quota: QuotaSnapshotOut


class RouteGenerateOut(BaseModel):
    """Form channel returns route_id immediately; chat returns proposal first."""

    channel: GenerateChannel
    proposal: RouteProposalOut
    route_id: str | None = None
    persisted_draft: bool = False


SessionStatus = Literal["active", "closed"]
ChatMessageRole = Literal["user", "assistant", "system"]
ChatIntentOut = Literal[
    "crisis",
    "greeting",
    "on_topic_travel",
    "off_topic",
    "injection_attempt",
    "generate",
]


class RoutePlanningSessionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: RouteMatchParamsIn
    confirmed_fields: list[str] = Field(default_factory=list, max_length=24)

    @field_validator("confirmed_fields")
    @classmethod
    def clean_confirmed(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in value:
            item = raw.strip()
            if not item or item in seen:
                continue
            if len(item) > 40:
                raise ValueError("confirmed field name too long")
            seen.add(item)
            cleaned.append(item)
            if len(cleaned) >= 24:
                break
        return cleaned


class RoutePlanningSessionOut(BaseModel):
    session_id: str
    status: SessionStatus
    constraints: RouteMatchParamsIn
    confirmed_fields: list[str] = Field(default_factory=list)
    ai_planning_enabled: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # How full this chat is. The app warns near the end and stops the
    # composer once `message_count` reaches `message_limit`.
    message_count: int = 0
    message_limit: int = 0


class RoutePlanningSessionListOut(BaseModel):
    items: list[RoutePlanningSessionOut]
    total: int
    limit: int
    offset: int


class RoutePlanningStoredMessageOut(BaseModel):
    message_id: str
    session_id: str
    role: ChatMessageRole
    text: str
    intent: ChatIntentOut | None = None
    proposal_id: str | None = None
    blocks: list[ChatBlockOut] = Field(default_factory=list)
    created_at: datetime


class RoutePlanningMessageListOut(BaseModel):
    items: list[RoutePlanningStoredMessageOut]
    total: int
    limit: int
    offset: int


class ChatControlsIn(BaseModel):
    """One explicit confirmation, including unchanged values and false toggles."""

    model_config = ConfigDict(extra="forbid")

    city: str | None = Field(default=None, min_length=1, max_length=80)
    budget_amount: StrictInt | None = Field(default=None, ge=0, le=1_000_000)
    with_children: StrictBool | None = None
    with_pets: StrictBool | None = None
    avoid_crowds: StrictBool | None = None

    @field_validator("city")
    @classmethod
    def clean_city(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("City must not be blank")
        return value.strip() if value is not None else None


class RoutePlanningMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    want_generate: bool = False
    action_id: str | None = Field(default=None, max_length=64)
    control_value: float | bool | None = None
    controls: ChatControlsIn | None = None

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Value must not be blank")
        return cleaned

    @field_validator("action_id")
    @classmethod
    def strip_action_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class RoutePlanningMessageOut(BaseModel):
    # Carried on every reply so the app learns a chat filled up from the
    # answer itself, without polling the session.
    session_status: SessionStatus = "active"
    session_message_count: int = 0
    session_message_limit: int = 0

    message_id: str
    session_id: str
    role: ChatMessageRole
    text: str
    intent: ChatIntentOut | None = None
    proposed_constraints: dict[str, object] | None = None
    confirmed_fields: list[str] = Field(default_factory=list)
    ask_field: str | None = None
    proposal: RouteProposalOut | None = None
    blocks: list[ChatBlockOut] = Field(default_factory=list)
    provider: str | None = None
    fallback: bool = False
