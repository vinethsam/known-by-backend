"""Shared retrieval contracts and safe error types."""

from __future__ import annotations

import math
import random
import socket
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


class FetchError(RuntimeError):
    """Raised when retrieval cannot return a valid public result."""

    transient = False
    infrastructure = False
    response_uncertain = False
    retry_after_seconds: float | None = None


class FetchTransientError(FetchError):
    """A temporary retrieval failure, distinct from URL policy or configuration."""

    transient = True

    def __init__(
        self, message: str, *, infrastructure: bool = False, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(message)
        self.infrastructure = infrastructure
        self.retry_after_seconds = retry_after_seconds


class FetchConfigurationError(FetchError):
    """Raised when retrieval configuration is missing or invalid."""


class FetchTimeoutError(FetchTransientError):
    """Raised when retrieval times out."""


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    return max(min(12.0, 0.75 * (2**attempt)) + random.uniform(0, 0.2), retry_after or 0)


def permanent_network_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current, (httpx.UnsupportedProtocol, httpx.LocalProtocolError, ssl.SSLCertVerificationError)
        ):
            return True
        if isinstance(current, socket.gaierror) and current.errno != socket.EAI_AGAIN:
            return True
        current = current.__cause__ or current.__context__
    return False


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
