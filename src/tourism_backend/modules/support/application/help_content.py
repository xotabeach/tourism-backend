"""Reviewed source-pack contract; not an API, index or publication command."""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")]


class HelpArticleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Slug
    revision: int = Field(ge=1)
    category: Literal["routes", "app", "travel_points"]
    faq_id: Slug
    title: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=180)
    body_file: str = Field(pattern=r"^articles/[a-z][a-z0-9-]*\.md$")
    evidence: list[str] = Field(min_length=1, max_length=8)


class HelpManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    corpus_id: Slug
    language: Literal["ru"]
    target_app_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Literal["draft", "published", "withdrawn"]
    release_verified: bool
    approved_by: str | None = Field(default=None, min_length=3, max_length=100)
    code_verified_on: date
    backend_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    mobile_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    articles: list[HelpArticleSpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _validate_pack(self) -> Self:
        if self.status == "published" and (
            not self.release_verified or not (self.approved_by or "").strip()
        ):
            raise ValueError("Publication needs release verification and editorial approval")
        for keys in (
            [item.id for item in self.articles],
            [f"{item.category}/{item.faq_id}" for item in self.articles],
            [item.body_file for item in self.articles],
        ):
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate article, FAQ route or body file")
        return self
