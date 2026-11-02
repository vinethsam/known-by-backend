"""Explicit, one-request production diagnostic; never imported by worker startup."""

from __future__ import annotations

import asyncio
import html
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


class _DiagnosticClient(OpenRouterClient):
    """Observe the decoded envelope after the existing bounded parser reads it."""

    def __init__(self, settings, http, details, secret):
        super().__init__(settings, http)
        self.details, self.secret = details, secret

    async def _read_json_response(self, response):
        original_iterator = response.aiter_bytes
        prefix = bytearray()

        async def observe_decoded(*args, **kwargs):
            async for chunk in original_iterator(*args, **kwargs):
                if len(prefix) < 8192:
                    prefix.extend(chunk[: 8192 - len(prefix)])
                yield chunk

        response.aiter_bytes = observe_decoded
        try:
            data = await super()._read_json_response(response)
        except OpenRouterError:
            text = prefix.decode("utf-8", errors="replace")
            title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", text, re.IGNORECASE | re.DOTALL)
            if title:
                plain_title = html.unescape(re.sub(r"<[^>]*>", " ", title.group(1)))
                self.details["html_title"] = sanitize_diagnostic(plain_title, self.secret, 200)
            if str(self.details.get("cf_mitigated", "")).casefold() == "challenge":
                category = "cloudflare_challenge"
            elif "cloudflare" in text.casefold() and (
                "blocked" in text.casefold() or "attention required" in text.casefold()
            ):
                category = "cloudflare_access_denied_page"
            else:
                category = "html_error_page" if title else "non_json_error_response"
            self.details["response_category"] = category
            raise
        else:
            self.details.update(_error_details(data, self.secret))
            return data
        finally:
            response.aiter_bytes = original_iterator
            prefix.clear()


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
        for header, field in (
            ("content-type", "response_content_type"),
            ("content-encoding", "response_content_encoding"),
            ("server", "server"),
            ("cf-mitigated", "cf_mitigated"),
        ):
            details[field] = sanitize_diagnostic(response.headers.get(header), secret, 120)
        request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("x-openrouter-request-id")
            or response.headers.get("cf-ray")
        )
        details["request_id"] = sanitize_diagnostic(request_id, secret, 128)

    usage = []
    try:
        async with httpx.AsyncClient(
            timeout=settings.OPENROUTER_TIMEOUT_SECONDS,
            trust_env=False,
            transport=transport,
            event_hooks={"response": [observe]},
        ) as http:
            client = _DiagnosticClient(settings, http, details, secret)
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
