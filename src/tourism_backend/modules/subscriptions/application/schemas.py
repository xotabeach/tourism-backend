"""Travel+ request DTOs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TravelPlusPlan = Literal["monthly", "yearly"]


class TravelPlusActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: TravelPlusPlan = Field(description="Billing period for the mock grant")
