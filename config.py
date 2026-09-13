"""Environment-backed configuration for the retrieval service."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os


DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0


def _optional_environment_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None

    stripped_value = value.strip()
    return stripped_value or None


def _fetch_timeout_seconds() -> float:
    raw_value = _optional_environment_value("FETCH_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_FETCH_TIMEOUT_SECONDS

    try:
        timeout = float(raw_value)
    except ValueError as exc:
        raise ValueError("FETCH_TIMEOUT_SECONDS must be a number") from exc

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("FETCH_TIMEOUT_SECONDS must be greater than zero")

    return timeout


@dataclass(frozen=True, slots=True)
class Settings:
    STATIC_FETCH_WORKER_URL: str | None
    STATIC_FETCH_WORKER_SECRET: str | None
    FETCH_TIMEOUT_SECONDS: float


settings = Settings(
    STATIC_FETCH_WORKER_URL=_optional_environment_value(
        "STATIC_FETCH_WORKER_URL"
    ),
    STATIC_FETCH_WORKER_SECRET=_optional_environment_value(
        "STATIC_FETCH_WORKER_SECRET"
    ),
    FETCH_TIMEOUT_SECONDS=_fetch_timeout_seconds(),
)
