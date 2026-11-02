"""Explicit, one-request production diagnostic; never imported by worker startup."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.providers.openrouter import OpenRouterClient, OpenRouterError
from app.providers.search import OpenRouterSearchProvider

DIAGNOSTIC_QUERY = "Royal Academy of Arts architecture official"


def sanitize_diagnostic(value: Any, secret: str = "", limit: int = 600) -> str | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = str(value)
    for key in {secret, secret.strip()} - {""}:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\bsk[-_][a-z0-9_-]{6,}", "[REDACTED]", text)
    text = re.sub(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED]", text)
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    return " ".join(text.split())[:limit]


def _error_details(data: Any, secret: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    error = data.get("error")
    if not error and isinstance(data.get("choices"), list):
        error = next(
            (
                choice.get("error")
                for choice in data["choices"]
                if isinstance(choice, dict) and choice.get("error")
            ),
            None,
        )
    if isinstance(error, str):
        return {"provider_error_message": sanitize_diagnostic(error, secret)}
    if not isinstance(error, dict):
        return {}
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "provider_error_code": sanitize_diagnostic(error.get("code"), secret, 80),
        "provider_error_message": sanitize_diagnostic(error.get("message"), secret),
        "provider_name": sanitize_diagnostic(
            error.get("provider_name") or metadata.get("provider_name"), secret, 120
        ),
    }


class _DiagnosticStream(httpx.AsyncByteStream):
    """Observe bytes already read by the production bounded response parser."""

    def __init__(self, inner, limit, details, secret):
        self.inner, self.limit, self.details, self.secret = inner, limit, details, secret
        self.parts: list[bytes] = []
        self.size = 0
        self.oversized = False

    async def __aiter__(self):
        async for chunk in self.inner:
            self.size += len(chunk)
            if self.size <= self.limit:
                self.parts.append(chunk)
            else:
                self.oversized = True
                self.parts.clear()
            yield chunk

    async def aclose(self):
        try:
            if not self.oversized:
                try:
                    data = json.loads(b"".join(self.parts))
                except (ValueError, UnicodeDecodeError):
                    pass
                else:
                    self.details.update(_error_details(data, self.secret))
        finally:
            self.parts.clear()
            await self.inner.aclose()


async def run_diagnostic(settings: Settings | None = None, *, transport=None) -> dict[str, Any]:
    settings = (settings or get_settings()).model_copy(update={"OPENROUTER_MAX_RETRIES": 0})
    secret = settings.OPENROUTER_API_KEY.get_secret_value() if settings.OPENROUTER_API_KEY else ""
    details: dict[str, Any] = {
        "event": "provider_diagnostic",
        "operation": "web_search",
        "source_model": settings.OPENROUTER_SOURCE_MODEL,
        "extraction_model": settings.OPENROUTER_EXTRACTION_MODEL,
        "maximum_requests": 1,
        "result_limit": 1,
        "http_status": None,
        "candidate_count": 0,
        "parse_outcome": "not_attempted",
    }

    async def observe(response):
        details["http_status"] = response.status_code
        request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("x-openrouter-request-id")
            or response.headers.get("cf-ray")
        )
        details["request_id"] = sanitize_diagnostic(request_id, secret, 128)
        response.stream = _DiagnosticStream(response.stream, settings.MAX_RESPONSE_BYTES, details, secret)

    usage = []
    try:
        async with httpx.AsyncClient(
            timeout=settings.OPENROUTER_TIMEOUT_SECONDS,
            trust_env=False,
            transport=transport,
            event_hooks={"response": [observe]},
        ) as http:
            client = OpenRouterClient(settings, http)
            try:
                data, usage = await client.web_search(DIAGNOSTIC_QUERY, 1, {"diagnostic": "provider_access"})
            except OpenRouterError as exc:
                usage = exc.usage
                details["error_code"] = exc.error_code
                details["parse_outcome"] = (
                    "provider_rejected" if exc.http_status and exc.http_status >= 400 else "request_failed"
                )
            else:
                candidates = OpenRouterSearchProvider(settings, client)._parse_results(data, 1)
                details["candidate_count"] = len(candidates)
                details["parse_outcome"] = "valid_candidates" if candidates else "valid_no_candidates"
    except Exception as exc:
        # No exception text, credentials, raw payload or diagnostic seed enters logs.
        details["parse_outcome"] = "diagnostic_failed"
        details["exception_type"] = type(exc).__name__
    details.update(
        usage_records=len(usage),
        prompt_tokens=sum(record.prompt_tokens for record in usage),
        completion_tokens=sum(record.completion_tokens for record in usage),
        reported_cost=sum(record.cost for record in usage)
        if usage and all(record.cost is not None for record in usage)
        else None,
        tool_requests=sum(record.web_search_requests for record in usage)
        if usage and all(record.web_search_requests is not None for record in usage)
        else None,
    )
    return details


def main() -> None:
    print(json.dumps(asyncio.run(run_diagnostic()), ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
