from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PublicAchievementOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    slug: str = Field(max_length=64)
    title: str = Field(max_length=120)
    description: str = Field(max_length=240)
    how_to_earn: str = Field(default="", max_length=240)
    icon_slug: str = Field(default="", max_length=64)
    is_unlocked: bool = True


class AchievementProgress(BaseModel):
    current: float = Field(ge=0)
    target: float = Field(gt=0)


class AchievementOut(PublicAchievementOut):
    is_unlocked: bool = False
    status: Literal["unlocked", "locked", "soon"] = "locked"
    unlocked_at: datetime | None = None
    progress: AchievementProgress | None = None
    celebrated: bool = False
    available: bool = True


class AchievementListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AchievementOut]
    unlocked_count: int = Field(ge=0)
    total: int = Field(ge=0)


class PublicAchievementListOut(BaseModel):
    items: list[PublicAchievementOut]
    unlocked_count: int = Field(ge=0)
    total: int = Field(ge=0)


class AchievementsCelebratedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    achievement_ids: list[UUID] = Field(max_length=100)
