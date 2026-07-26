"""Validated request and response models for page API contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CreatePageRequest", "CreatePageResponse"]


class CreatePageRequest(BaseModel):
    """Payload accepted by Docmost's page creation endpoint."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    title: str
    space_id: str = Field(serialization_alias="spaceId")
    content: str
    format: Literal["markdown"] = "markdown"
    parent_page_id: str | None = Field(default=None, serialization_alias="parentPageId")
    icon: str | None = None


class CreatePageResponse(BaseModel):
    """Validated page creation result returned by Docmost."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
