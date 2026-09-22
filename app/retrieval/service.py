"""Static retrieval, retrieval orchestration and structured source support.

The backend validates requested URLs before retrieval and validates Worker final
URLs after retrieval. The external static-fetch Worker must also enforce DNS and
redirect SSRF checks before each upstream request, because the backend cannot undo
a private redirect that the Worker already followed.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.research.telemetry import count
from app.retrieval.browser import (
    BrowserRenderer,
    BrowserRouteHTTPClient,
    PinnedAsyncHTTPTransport,
    PinnedPublicNetworkBackend,
)
from app.retrieval.contracts import (
    FetchConfigurationError,
    FetchError,
    FetchTimeoutError,
    RetrievedPage,
    content_type_allowed,
)
from app.retrieval.urls import (
    URLValidationError,
    canonicalise_url,
    domain_key,
    validate_public_url,
)

_BLOCKED_PAGE_RE = re.compile(
    r"\b(access denied|are you a robot|captcha|cf-challenge|login required|sign in to continue|verify you are human)\b",
    re.IGNORECASE,
)


def _secret_value(secret: Any) -> str | None:
    if secret is None:
        return None
    if hasattr(secret, "get_secret_value"):
        return str(secret.get_secret_value())
    return str(secret)


def _response_byte_limit(settings: object) -> int:
    return int(getattr(settings, "MAX_RESPONSE_BYTES", 2_000_000))


def _worker_response_limit(settings: object) -> int:
    return _response_byte_limit(settings) + 65_536


def _validate_worker_url(worker_url: str, settings: object) -> str:
    try:
        canonical = canonicalise_url(worker_url)
    except URLValidationError as exc:
        raise FetchConfigurationError("STATIC_FETCH_WORKER_URL is not a valid HTTP(S) URL") from exc

    parsed = urlsplit(canonical)
    if parsed.scheme == "http" and getattr(settings, "APP_ENV", "development") == "production":
        raise FetchConfigurationError("Production Worker URL must use HTTPS")
    if parsed.username or parsed.password:
        raise FetchConfigurationError("STATIC_FETCH_WORKER_URL must not include credentials")
    return canonical


async def _validate_target(url: str, settings: object) -> str:
    try:
        return await validate_public_url(url, settings)
    except URLValidationError as exc:
        raise FetchError(str(exc) or "URL is not eligible for retrieval") from exc


class StaticFetcher:
    """Client for the configured static-fetch Worker."""

    def __init__(self, settings: object, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    async def _client_for_request(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=float(getattr(self.settings, "FETCH_TIMEOUT_SECONDS", 30)),
                limits=httpx.Limits(
                    max_connections=int(getattr(self.settings, "MAX_CONCURRENT_FETCHES", 4)),
                    max_keepalive_connections=int(getattr(self.settings, "MAX_CONCURRENT_FETCHES", 4)),
                ),
                trust_env=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str) -> RetrievedPage:
        target_url = await _validate_target(url, self.settings)
        worker_url = getattr(self.settings, "STATIC_FETCH_WORKER_URL", None)
        if not worker_url:
            raise FetchConfigurationError("STATIC_FETCH_WORKER_URL is not configured")
        worker_url = _validate_worker_url(str(worker_url), self.settings)

        headers: dict[str, str] = {}
        secret = _secret_value(getattr(self.settings, "STATIC_FETCH_WORKER_SECRET", None))
        if secret:
            headers["X-Worker-Secret"] = secret

        client = await self._client_for_request()
        raw = bytearray()
        try:
            async with client.stream(
                "POST", worker_url, json={"url": target_url}, headers=headers
            ) as response:
                if response.status_code >= 400:
                    raise FetchError(f"The static-fetch Worker returned HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _worker_response_limit(self.settings):
                        raise FetchError("The static-fetch Worker response exceeded the size limit")
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError("The static-fetch Worker request timed out") from exc
        except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
            raise FetchConfigurationError("STATIC_FETCH_WORKER_URL is not a valid HTTP(S) URL") from exc
        except httpx.RequestError as exc:
            raise FetchError("Could not connect to the static-fetch Worker") from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FetchError("The static-fetch Worker returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise FetchError("The static-fetch Worker returned an unexpected JSON value")

        try:
            page = RetrievedPage.model_validate(payload)
        except ValidationError as exc:
            raise FetchError("The static-fetch Worker returned malformed response data") from exc

        page.requested_url = await _validate_target(page.requested_url, self.settings)
        page.final_url = await _validate_target(page.final_url, self.settings)
        page.retrieval_method = "static"
        if not content_type_allowed(page.content_type):
            raise FetchError("The retrieved content type is not supported")
        if len(page.html.encode("utf-8")) > _response_byte_limit(self.settings):
            raise FetchError("The retrieved content exceeded the size limit")
        return page


def needs_browser(page: RetrievedPage, settings: object) -> bool:
    """Return true only for successful, thin JavaScript application shells."""

    if not getattr(settings, "PLAYWRIGHT_ENABLED", True):
        return False
    if page.status < 200 or page.status >= 300:
        return False
    if page.content_type and "html" not in page.content_type.lower():
        return False
    if _BLOCKED_PAGE_RE.search(page.html or ""):
        return False

    soup = BeautifulSoup(page.html or "", "html.parser")
    visible_text = soup.get_text(" ", strip=True)
    readable_chars = len(visible_text)
    min_chars = int(getattr(settings, "STATIC_MIN_READABLE_CHARS", 250))
    script_count = len(soup.find_all("script"))
    has_shell_root = bool(soup.find(id=re.compile(r"^(app|root|__next|___gatsby)$", re.IGNORECASE)))
    has_noscript_app_hint = (
        "javascript" in " ".join(node.get_text(" ", strip=True) for node in soup.find_all("noscript")).lower()
    )
    return readable_chars < min_chars and (script_count >= 2 or has_shell_root or has_noscript_app_hint)


class RetrievalService:
    """Coordinates cache, concurrency, static retrieval and browser fallback."""

    def __init__(
        self,
        settings: object,
        static_fetcher: StaticFetcher | None = None,
        renderer: BrowserRenderer | None = None,
        cache: Any | None = None,
    ) -> None:
        self.settings = settings
        self.static_fetcher = static_fetcher or StaticFetcher(settings)
        self.renderer = (
            renderer
            if renderer is not None
            else (BrowserRenderer(settings) if getattr(settings, "PLAYWRIGHT_ENABLED", True) else None)
        )
        self.cache = cache
        self._global_fetch_sem = asyncio.Semaphore(int(getattr(settings, "MAX_CONCURRENT_FETCHES", 4)))
        self._domain_sems: dict[str, asyncio.Semaphore] = {}
        self._domain_users: dict[str, int] = {}
        self._locks: dict[tuple[str | None, str], asyncio.Lock] = {}
        self._lock_users: dict[tuple[str | None, str], int] = {}

    async def close(self) -> None:
        await self.static_fetcher.close()
        if self.renderer is not None:
            await self.renderer.close()

    def _domain_sem(self, key: str) -> asyncio.Semaphore:
        if key not in self._domain_sems:
            self._domain_sems[key] = asyncio.Semaphore(
                int(getattr(self.settings, "PER_DOMAIN_CONCURRENCY", 1))
            )
        return self._domain_sems[key]

    def _url_lock(self, job_id: str | None, key: str) -> tuple[tuple[str | None, str], asyncio.Lock]:
        lock_key = (job_id, key)
        if lock_key not in self._locks:
            self._locks[lock_key] = asyncio.Lock()
        self._lock_users[lock_key] = self._lock_users.get(lock_key, 0) + 1
        return lock_key, self._locks[lock_key]

    @asynccontextmanager
    async def _fetch_slot(self, key: str) -> AsyncIterator[None]:
        per_domain = self._domain_sem(key)
        self._domain_users[key] = self._domain_users.get(key, 0) + 1
        try:
            # Domain waiters must not occupy global slots needed by other domains.
            async with per_domain, self._global_fetch_sem:
                yield
        finally:
            self._domain_users[key] -= 1
            if self._domain_users[key] == 0:
                del self._domain_users[key]
                del self._domain_sems[key]

    async def _cache_get(self, job_id: str | None, key: str) -> RetrievedPage | None:
        if self.cache is None:
            return None
        try:
            if isinstance(self.cache, dict):
                value = self.cache.get((job_id, key))
            elif hasattr(self.cache, "get_cache"):
                if job_id is None:
                    return None
                value = await asyncio.to_thread(self.cache.get_cache, job_id, key)
            elif hasattr(self.cache, "get"):
                value = await asyncio.to_thread(self.cache.get, (job_id, key))
            else:
                return None

            if value is None:
                return None
            if isinstance(value, RetrievedPage):
                page = value
            elif isinstance(value, str):
                page = RetrievedPage.model_validate(json.loads(value))
            elif isinstance(value, Mapping):
                page = RetrievedPage.model_validate(value)
            else:
                return None
            page.requested_url = await _validate_target(page.requested_url, self.settings)
            page.final_url = await _validate_target(page.final_url, self.settings)
            byte_limit = (
                int(getattr(self.settings, "BROWSER_MAX_TOTAL_BYTES", 8_000_000))
                if page.retrieval_method == "browser"
                else _response_byte_limit(self.settings)
            )
            if not content_type_allowed(page.content_type) or len(page.html.encode("utf-8")) > byte_limit:
                return None
            return page
        except (
            FetchError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ):
            return None

    async def _cache_put(self, job_id: str | None, key: str, page: RetrievedPage) -> None:
        if self.cache is None:
            return
        value = page.model_dump(mode="json")
        try:
            if isinstance(self.cache, dict):
                self.cache[(job_id, key)] = value
            elif hasattr(self.cache, "put_cache"):
                if job_id is not None:
                    await asyncio.to_thread(self.cache.put_cache, job_id, key, value)
            elif hasattr(self.cache, "set"):
                await asyncio.to_thread(self.cache.set, (job_id, key), value)
        except (OSError, RuntimeError, TypeError, ValueError, SQLAlchemyError):
            return

    async def retrieve(self, url: str, job_id: str | None = None) -> RetrievedPage:
        count("sources_requested")
        key_url = await _validate_target(url, self.settings)
        cache_key = canonicalise_url(key_url)
        lock_key, lock = self._url_lock(job_id, cache_key)
        try:
            async with lock:
                cached = await self._cache_get(job_id, cache_key)
                if cached is not None:
                    count("retrieval_cache_hits")
                    count("sources_retrieved")
                    return cached
                count("retrieval_cache_misses")

                async with self._fetch_slot(domain_key(cache_key)):
                    count("static_fetches")
                    page = await self.static_fetcher.fetch(key_url)
                    if needs_browser(page, self.settings) and self.renderer is not None:
                        count("playwright_fallbacks")
                        browser_page = await self.renderer.fetch(page.final_url)
                        page = browser_page.model_copy(update={"requested_url": key_url})
                await self._cache_put(job_id, cache_key, page)
                count("sources_retrieved")
                return page
        finally:
            self._lock_users[lock_key] -= 1
            if self._lock_users[lock_key] == 0:
                del self._lock_users[lock_key]
                del self._locks[lock_key]

    async def retrieve_structured(
        self, plan: Mapping[str, Any], seed: object, job_id: str | None = None
    ) -> list[RetrievedPage]:
        return await retrieve_structured(plan, seed, self, job_id=job_id)


def _seed_value(seed: object, key: str) -> Any:
    if isinstance(seed, Mapping):
        return seed.get(key)
    return getattr(seed, key, None)


def _expand_params(params: Mapping[str, Any] | None, seed: object) -> dict[str, str]:
    expanded: dict[str, str] = {}
    for name, value in (params or {}).items():
        if isinstance(value, str) and value.startswith("$"):
            value = _seed_value(seed, value[1:])
        if value is not None:
            expanded[name] = str(value)
    return expanded


def _url_with_params(url: str, params: Mapping[str, Any] | None, seed: object) -> str:
    expanded = _expand_params(params, seed)
    if not expanded:
        return url
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    pairs.extend(expanded.items())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), parsed.fragment))


def _json_from_page(page: RetrievedPage) -> Any:
    if page.captured_json is not None:
        return page.captured_json
    if page.content_type and "json" in page.content_type.lower():
        return json.loads(page.html)
    return None


def _path_get(data: Any, path: str | None) -> Any:
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)) and part.isdigit():
            current = current[int(part)]
        else:
            return None
    return current


def _name_tokens(seed: object) -> list[str]:
    full_name = str(_seed_value(seed, "full_name") or "").lower()
    return [part for part in re.split(r"\W+", full_name) if len(part) > 1]


def _record_matches(record: Any, seed: object, fields: Sequence[str] | None = None) -> bool:
    if not isinstance(record, Mapping):
        return False
    tokens = _name_tokens(seed)
    if not tokens:
        return True
    fields = fields or ("full_name", "name", "title", "profile", "description", "bio")
    haystack = " ".join(str(_path_get(record, field) or "") for field in fields).lower()
    return all(token in haystack for token in tokens)


def _filtered_page(page: RetrievedPage, records: list[Any]) -> RetrievedPage:
    payload = {"records": records}
    return page.model_copy(
        update={
            "captured_json": payload,
            "html": json.dumps(payload, ensure_ascii=False),
        }
    )


async def retrieve_structured(
    plan: Mapping[str, Any],
    seed: object,
    service: RetrievalService,
    job_id: str | None = None,
) -> list[RetrievedPage]:
    """Retrieve an operator-provided structured source plan.

    Supported plan modes are ``filtered``, ``bulk`` and ``paginated``. URLs and
    field paths must come from operator configuration.

    Common plan fields:
    - ``mode`` / ``type``: ``filtered``, ``bulk`` or ``paginated``.
    - ``url`` or ``urls``: operator-provided source endpoints.
    - ``params``: query parameters; values like ``"$full_name"`` are copied from
      the person seed.
    - ``record_path`` / ``records_path``: dot path to records in JSON payloads.
    - ``filter_fields``: optional dot paths searched for local person filtering.
    - ``page_param`` / ``start_page`` / ``max_pages``: generic pagination bounds.
    """

    mode = str(plan.get("mode") or plan.get("type") or "filtered").lower()
    max_pages = min(
        int(plan.get("max_pages") or getattr(service.settings, "MAX_STRUCTURED_PAGES", 5)),
        int(getattr(service.settings, "MAX_STRUCTURED_PAGES", 5)),
    )
    if max_pages < 1:
        raise FetchConfigurationError("Structured retrieval requires at least one page")

    if mode == "filtered":
        url = plan.get("url")
        if not isinstance(url, str) or not url:
            raise FetchConfigurationError("Structured filtered plans require a URL")
        return [await service.retrieve(_url_with_params(url, plan.get("params"), seed), job_id=job_id)]

    if mode == "bulk":
        urls = plan.get("urls") or ([plan["url"]] if isinstance(plan.get("url"), str) else None)
        if not isinstance(urls, Sequence) or isinstance(urls, (str, bytes)):
            raise FetchConfigurationError("Structured bulk plans require URL values")
        pages = [await service.retrieve(str(url), job_id=job_id) for url in list(urls)[:max_pages]]
        record_path = str(plan.get("record_path") or plan.get("records_path") or "")
        filter_fields = plan.get("filter_fields")
        filtered: list[RetrievedPage] = []
        for page in pages:
            data = _json_from_page(page)
            records = _path_get(data, record_path)
            if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
                matching = [record for record in records if _record_matches(record, seed, filter_fields)]
                filtered.append(_filtered_page(page, matching))
            else:
                filtered.append(page)
        return filtered

    if mode == "paginated":
        url = plan.get("url")
        page_param = str(plan.get("page_param") or "page")
        start_page = int(plan.get("start_page") or 1)
        record_path = str(plan.get("record_path") or plan.get("records_path") or "")
        pages: list[RetrievedPage] = []
        for offset in range(max_pages):
            current_url = _url_with_params(
                str(url),
                {**dict(plan.get("params") or {}), page_param: start_page + offset},
                seed,
            )
            page = await service.retrieve(current_url, job_id=job_id)
            pages.append(page)
            records = _path_get(_json_from_page(page), record_path)
            if isinstance(records, Sequence) and not isinstance(records, (str, bytes)) and not records:
                break
        return pages

    raise FetchConfigurationError("Unsupported structured retrieval mode")


__all__ = [
    "BrowserRenderer",
    "BrowserRouteHTTPClient",
    "FetchConfigurationError",
    "FetchError",
    "FetchTimeoutError",
    "PinnedAsyncHTTPTransport",
    "PinnedPublicNetworkBackend",
    "RetrievalService",
    "RetrievedPage",
    "StaticFetcher",
    "needs_browser",
    "retrieve_structured",
]
