"""Search provider adapters."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from typing import Protocol, Self
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.schemas import SourceCandidate

BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_BRAVE_RESULTS = 20
MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 700


class SearchProviderError(RuntimeError):
    """Raised when a bounded search attempt cannot complete."""


class SearchResponseTooLarge(SearchProviderError):
    """Raised when a search provider response exceeds the configured byte cap."""


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[SourceCandidate]:
        """Return bounded search candidates for a query."""


class BraveSearchProvider:
    """Brave Web Search adapter using the documented GET endpoint."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        max_retries: int = 2,
        sleep: Callable[[float], object] | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._max_retries = max(0, max_retries)
        self._sleep = sleep or asyncio.sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.settings.SEARCH_TIMEOUT_SECONDS)
        return self._client

    async def search(self, query: str, limit: int) -> list[SourceCandidate]:
        query = " ".join(query.split())
        if not query:
            return []
        if not self.settings.BRAVE_SEARCH_API_KEY:
            raise SearchProviderError("BRAVE_SEARCH_API_KEY is required")

        count = min(max(1, limit), MAX_BRAVE_RESULTS, self.settings.MAX_SEARCH_RESULTS_PER_QUERY)
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.settings.BRAVE_SEARCH_API_KEY.get_secret_value(),
        }
        params = {
            "q": query[:600],
            "count": count,
            "result_filter": "web",
            "safesearch": "moderate",
            "text_decorations": False,
        }

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            await self._wait_for_interval()
            try:
                request = self.client.build_request(
                    "GET", BRAVE_WEB_SEARCH_URL, params=params, headers=headers
                )
                response = await self.client.send(request, stream=True)
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    await response.aclose()
                    raise httpx.HTTPStatusError("Brave request rejected", request=request, response=response)
                data = await self._read_json_response(response)
                if response.status_code in {429} or 500 <= response.status_code <= 599:
                    raise SearchProviderError(f"Brave search retryable HTTP {response.status_code}")
                response.raise_for_status()
                return self._parse_results(data, count)
            except (httpx.TimeoutException, httpx.TransportError, SearchProviderError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                await self._sleep(self._retry_delay(attempt))
            except httpx.HTTPStatusError as exc:
                raise SearchProviderError(f"Brave search HTTP {exc.response.status_code}") from exc
            except (ValueError, TypeError) as exc:
                raise SearchProviderError("Brave search returned malformed JSON") from exc

        raise SearchProviderError("Brave search failed after bounded retries") from last_error

    async def _wait_for_interval(self) -> None:
        min_interval = self.settings.SEARCH_MIN_INTERVAL_SECONDS
        if min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self._next_request_at - now)
            if wait_for:
                await self._sleep(wait_for)
            self._next_request_at = time.monotonic() + min_interval

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        return min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.15)

    @staticmethod
    def _parse_results(data: dict, limit: int) -> list[SourceCandidate]:
        if not isinstance(data, dict):
            raise SearchProviderError("Brave search returned malformed JSON envelope")
        web = data.get("web", {})
        if not isinstance(web, dict):
            raise SearchProviderError("Brave search returned malformed web results")
        results = web.get("results", [])
        if not isinstance(results, list):
            raise SearchProviderError("Brave search returned malformed results list")
        results = results[:limit]
        candidates: list[SourceCandidate] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            candidates.append(
                SourceCandidate(
                    url=url,
                    title=str(item.get("title") or "")[:MAX_TITLE_CHARS],
                    snippet=str(item.get("description") or "")[:MAX_SNIPPET_CHARS],
                    domain=parsed.hostname.lower().removeprefix("www.").rstrip("."),
                    origin="brave",
                )
            )
        return candidates

    async def _read_json_response(self, response: httpx.Response) -> object:
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self.settings.MAX_RESPONSE_BYTES:
                    raise SearchResponseTooLarge("Brave search response too large")
                chunks.append(chunk)
        finally:
            await response.aclose()
        raw = b"".join(chunks)
        if not raw.strip():
            raise SearchProviderError("Brave search returned empty response")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SearchProviderError("Brave search returned malformed JSON") from exc
