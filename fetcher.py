"""Asynchronous client for the static-fetch Worker."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from config import settings
from schemas import FetchResult


class FetchError(RuntimeError):
    """Raised when the static-fetch Worker cannot return a valid result."""


class FetchConfigurationError(FetchError):
    """Raised when retrieval configuration is missing or invalid."""


class FetchTimeoutError(FetchError):
    """Raised when the static-fetch Worker does not respond in time."""


async def fetch_url(url: str) -> FetchResult:
    """Ask the configured Worker to retrieve a target URL."""

    worker_url = settings.STATIC_FETCH_WORKER_URL
    if worker_url is None:
        raise FetchConfigurationError(
            "STATIC_FETCH_WORKER_URL is not configured"
        )

    headers: dict[str, str] = {}
    if settings.STATIC_FETCH_WORKER_SECRET is not None:
        headers["X-Worker-Secret"] = settings.STATIC_FETCH_WORKER_SECRET

    try:
        async with httpx.AsyncClient(
            timeout=settings.FETCH_TIMEOUT_SECONDS
        ) as client:
            response = await client.post(
                worker_url,
                json={"url": url},
                headers=headers,
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise FetchTimeoutError(
            "The static-fetch Worker request timed out"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise FetchError(
            "The static-fetch Worker returned HTTP "
            f"{exc.response.status_code}"
        ) from exc
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        raise FetchConfigurationError(
            "STATIC_FETCH_WORKER_URL is not a valid URL"
        ) from exc
    except httpx.RequestError as exc:
        raise FetchError(
            "Could not connect to the static-fetch Worker"
        ) from exc

    try:
        payload: Any = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FetchError(
            "The static-fetch Worker returned invalid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise FetchError(
            "The static-fetch Worker returned an unexpected JSON value"
        )

    try:
        return FetchResult(**payload)
    except ValidationError as exc:
        raise FetchError(
            "The static-fetch Worker returned malformed response data"
        ) from exc
