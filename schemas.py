"""Pydantic contracts for static retrieval and page processing."""

from typing import Annotated

from pydantic import AnyHttpUrl, BaseModel, Field


HttpStatus = Annotated[int, Field(strict=True, ge=100, le=599)]


class FetchRequest(BaseModel):
    url: AnyHttpUrl


class FetchResult(BaseModel):
    requested_url: str
    final_url: str
    status: HttpStatus
    content_type: str | None = None
    html: str


class ProcessedPage(BaseModel):
    requested_url: str
    final_url: str
    status: HttpStatus
    content_type: str | None = None
    markdown: str
