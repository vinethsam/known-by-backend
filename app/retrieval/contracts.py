"""Shared retrieval contracts and safe error types."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FetchError(RuntimeError):
    """Raised when retrieval cannot return a valid public result."""


class FetchConfigurationError(FetchError):
    """Raised when retrieval configuration is missing or invalid."""


class FetchTimeoutError(FetchError):
    """Raised when retrieval times out."""


class RetrievedPage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    requested_url: str
    final_url: str
    status: int = Field(ge=100, le=599)
    content_type: str | None = None
    html: str = ""
    retrieval_method: str = "static"
    captured_json: Any | None = None


ALLOWED_TARGET_CONTENT_TYPES = (
    "application/json",
    "application/ld+json",
    "application/xhtml+xml",
    "text/html",
    "text/json",
    "text/plain",
)


def content_type_allowed(content_type: str | None) -> bool:
    if not content_type:
        return True
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type in ALLOWED_TARGET_CONTENT_TYPES or media_type.endswith("+json")
