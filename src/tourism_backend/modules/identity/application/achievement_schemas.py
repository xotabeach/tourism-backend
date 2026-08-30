from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AchievementOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str = Field(max_length=64)
    title: str = Field(max_length=120)
    description: str = Field(max_length=240)
    how_to_earn: str = Field(default="", max_length=240)
    icon_slug: str = Field(default="", max_length=64)
    is_unlocked: bool = False
    unlocked_at: datetime | None = None


class AchievementListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AchievementOut]
    unlocked_count: int = Field(ge=0)
    total: int = Field(ge=0)
