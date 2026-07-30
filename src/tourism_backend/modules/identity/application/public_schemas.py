"""Public user profile read models."""

from pydantic import BaseModel, ConfigDict


class PublicUserOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    avatar_url: str | None = None
    cover_url: str | None = None
