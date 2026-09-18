"""Discover candidates exclusively from OpenRouter's web citation metadata."""

from __future__ import annotations

import ipaddress
from typing import Protocol
from urllib.parse import urlsplit

from app.config import Settings
from app.providers.openrouter import AttemptCallback, OpenRouterError, UsageCallback
from app.retrieval.urls import (
    _reject_blocked_hostname,
    _validate_public_ip,
    canonicalise_url,
)
from app.schemas import SourceCandidate, UsageRecord

MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 700


class SearchProviderError(RuntimeError):
    """A safe discovery failure retaining all known paid-attempt usage."""

    def __init__(self, message: str, *, usage: list[UsageRecord] | None = None) -> None:
        super().__init__(message)
        self.usage = usage or []


class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        limit: int,
        *,
        context: dict,
        before_attempt: AttemptCallback | None = None,
        on_usage: UsageCallback | None = None,
    ) -> list[SourceCandidate]:
        """Return bounded candidates backed by search-provider citations."""


class OpenRouterSearchProvider:
    """Use the shared OpenRouter client; this adapter owns no transport or API key."""

    def __init__(self, settings: Settings, client) -> None:
        self.settings = settings
        self.client = client

    async def search(
        self,
        query: str,
        limit: int,
        *,
        context: dict,
        before_attempt: AttemptCallback | None = None,
        on_usage: UsageCallback | None = None,
    ) -> list[SourceCandidate]:
        query = " ".join(query.split())[:600]
        if not query or limit < 1:
            return []
        count = min(limit, self.settings.MAX_SEARCH_RESULTS_PER_QUERY)
        try:
            data, _usage = await self.client.web_search(
                query, count, context, before_attempt=before_attempt, on_usage=on_usage
            )
        except OpenRouterError as exc:
            raise SearchProviderError(str(exc), usage=exc.usage) from exc
        return self._parse_results(data, count)

    @staticmethod
    def _parse_results(data: dict, limit: int) -> list[SourceCandidate]:
        # The shared client already validates the envelope before marking usage successful.
        # Deliberately never inspect message.content, tool_calls, or generated JSON URLs.
        candidates: list[SourceCandidate] = []
        seen: set[str] = set()
        for choice in data["choices"]:
            for annotation in choice["message"].get("annotations") or []:
                if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                    continue
                citation = annotation.get("url_citation")
                if not isinstance(citation, dict) or not isinstance(citation.get("url"), str):
                    continue
                try:
                    raw_url = citation["url"]
                    # Canonicalisation accepts bare domains for operator inputs; discovery
                    # must receive an explicit HTTP(S) URL in genuine citation metadata.
                    if urlsplit(raw_url).scheme.lower() not in {"http", "https"}:
                        continue
                    url = canonicalise_url(raw_url)
                    host = urlsplit(url).hostname or ""
                    _reject_blocked_hostname(host)
                    try:
                        literal_ip = ipaddress.ip_address(host)
                    except ValueError:
                        pass
                    else:
                        _validate_public_ip(literal_ip)
                except ValueError:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                title, content = citation.get("title"), citation.get("content")
                candidates.append(
                    SourceCandidate(
                        url=url,
                        title=title[:MAX_TITLE_CHARS] if isinstance(title, str) else "",
                        snippet=content[:MAX_SNIPPET_CHARS] if isinstance(content, str) else "",
                        domain=host.lower().removeprefix("www.").rstrip("."),
                        origin="openrouter_web_search",
                    )
                )
                if len(candidates) >= limit:
                    return candidates
        return candidates
