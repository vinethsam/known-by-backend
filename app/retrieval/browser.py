"""Guarded browser rendering and pinned browser-route HTTP transport."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend
from httpcore._backends.base import (
    SOCKET_OPTION,
    AsyncNetworkBackend,
    AsyncNetworkStream,
)

from app.retrieval.contracts import (
    ALLOWED_TARGET_CONTENT_TYPES,
    FetchConfigurationError,
    FetchError,
    FetchTimeoutError,
    RetrievedPage,
    content_type_allowed,
)
from app.retrieval.urls import URLValidationError, resolve_public_addresses

ALLOWED_BROWSER_CONTENT_TYPES = ALLOWED_TARGET_CONTENT_TYPES + (
    "application/ecmascript",
    "application/javascript",
    "application/x-javascript",
    "text/css",
    "text/ecmascript",
    "text/javascript",
)
BLOCKED_BROWSER_RESOURCE_TYPES = {"font", "image", "media"}
BROWSER_SECURITY_ARGS = (
    "--disable-webrtc",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
STRIPPED_FULFILL_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
STRIPPED_REQUEST_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
SAFE_BROWSER_METHODS = {"GET", "HEAD"}


def browser_content_type_allowed(content_type: str | None) -> bool:
    if not content_type:
        return True
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type in ALLOWED_BROWSER_CONTENT_TYPES or media_type.endswith("+json")


def response_byte_limit(settings: object) -> int:
    return int(getattr(settings, "MAX_RESPONSE_BYTES", 2_000_000))


async def validate_target(url: str, settings: object) -> str:
    from app.retrieval.urls import validate_public_url

    try:
        return await validate_public_url(url, settings)
    except URLValidationError as exc:
        raise FetchError(str(exc) or "URL is not eligible for retrieval") from exc


class PinnedPublicNetworkBackend(AsyncNetworkBackend):
    """Resolve once, reject any private answer, then connect to a vetted IP."""

    def __init__(
        self,
        settings: object,
        backend: AsyncNetworkBackend | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend or AnyIOBackend()
        self.connections: list[tuple[str, str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Sequence[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        try:
            addresses = await asyncio.to_thread(resolve_public_addresses, host, port)
        except URLValidationError as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        address = min(addresses)
        self.connections.append((host, address, port))
        return await self.backend.connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Sequence[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed for retrieval")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx transport backed by a public-address-pinned httpcore pool."""

    def __init__(self, settings: object) -> None:
        super().__init__(trust_env=False, http2=False)
        max_connections = int(getattr(settings, "BROWSER_MAX_REQUESTS", 80))
        self.network_backend = PinnedPublicNetworkBackend(settings)
        self._pool = httpcore.AsyncConnectionPool(
            max_connections=max_connections,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            network_backend=self.network_backend,
        )


@dataclass
class BrowserRouteResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str
    content_type: str | None


class BrowserRouteHTTPClient:
    """Bounded HTTP client used to fulfill every browser route."""

    def __init__(
        self,
        settings: object,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.total_bytes = 0
        self.redirect_counts: dict[str, int] = {}
        self._client = client
        self._owns_client = client is None

    async def _client_for_request(self) -> httpx.AsyncClient:
        if self._client is None:
            transport = PinnedAsyncHTTPTransport(self.settings)
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=float(getattr(self.settings, "BROWSER_TIMEOUT_SECONDS", 30)),
                transport=transport,
                trust_env=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
    ) -> BrowserRouteResponse:
        method = method.upper()
        if method not in SAFE_BROWSER_METHODS:
            raise FetchError("The browser route method is not supported")

        current_url = await validate_target(url, self.settings)
        max_redirects = int(getattr(self.settings, "MAX_REDIRECTS", 5))
        client = await self._client_for_request()
        request_headers = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower() not in STRIPPED_REQUEST_HEADERS
        }

        try:
            async with client.stream(
                method,
                current_url,
                headers=request_headers,
                follow_redirects=False,
            ) as response:
                if 300 <= response.status_code < 400 and response.headers.get("location"):
                    redirect_target = await validate_target(
                        urljoin(current_url, response.headers["location"]),
                        self.settings,
                    )
                    redirect_count = self.redirect_counts.get(current_url, 0)
                    if redirect_count >= max_redirects:
                        raise FetchError("The browser redirect limit was exceeded")
                    self.redirect_counts[redirect_target] = redirect_count + 1
                    fulfill_headers = {
                        name: value
                        for name, value in response.headers.items()
                        if name.lower() not in STRIPPED_FULFILL_HEADERS
                    }
                    fulfill_headers["location"] = redirect_target
                    return BrowserRouteResponse(
                        status=response.status_code,
                        headers=fulfill_headers,
                        body=b"",
                        final_url=redirect_target,
                        content_type=response.headers.get("content-type"),
                    )

                content_type = response.headers.get("content-type")
                if not browser_content_type_allowed(content_type):
                    raise FetchError("The browser retrieved an unsupported content type")

                body = bytearray()
                per_response_limit = response_byte_limit(self.settings)
                total_limit = int(getattr(self.settings, "BROWSER_MAX_TOTAL_BYTES", 8_000_000))
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    self.total_bytes += len(chunk)
                    if len(body) > per_response_limit:
                        raise FetchError("The browser response exceeded the size limit")
                    if self.total_bytes > total_limit:
                        raise FetchError("The browser total response size limit was exceeded")

                fulfill_headers = {
                    name: value
                    for name, value in response.headers.items()
                    if name.lower() not in STRIPPED_FULFILL_HEADERS
                }
                return BrowserRouteResponse(
                    status=response.status_code,
                    headers=fulfill_headers,
                    body=bytes(body),
                    final_url=current_url,
                    content_type=content_type,
                )
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError("The browser route request timed out") from exc
        except httpx.RequestError as exc:
            raise FetchError("The browser route request failed") from exc

        raise FetchError("The browser route request failed")


class BrowserRenderer:
    """Playwright renderer with public-request guards for dynamic pages."""

    def __init__(self, settings: object) -> None:
        self.settings = settings
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._browser_lock = asyncio.Lock()
        self._timeout_error: type[Exception] = TimeoutError

    async def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        async with self._browser_lock:
            if self._browser is not None:
                return self._browser
            try:
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise FetchConfigurationError("Playwright is not installed") from exc

            self._timeout_error = PlaywrightTimeoutError
            self._playwright = await async_playwright().start()
            try:
                args = list(BROWSER_SECURITY_ARGS)
                if not bool(getattr(self.settings, "BROWSER_SANDBOX", True)):
                    args.append("--no-sandbox")
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    chromium_sandbox=bool(getattr(self.settings, "BROWSER_SANDBOX", True)),
                    args=args,
                )
            except BaseException as exc:
                await self._playwright.stop()
                self._playwright = None
                if not isinstance(exc, Exception):
                    raise
                raise FetchError("Chromium could not launch") from exc
        return self._browser

    async def close(self) -> None:
        async with self._browser_lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def fetch(self, url: str) -> RetrievedPage:
        timeout_seconds = float(getattr(self.settings, "BROWSER_TIMEOUT_SECONDS", 30))
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self._fetch_once(url)
        except TimeoutError as exc:
            raise FetchTimeoutError("The browser retrieval timed out") from exc
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError("The browser retrieval failed") from exc

    async def _fetch_once(self, url: str) -> RetrievedPage:
        target_url = await validate_target(url, self.settings)
        browser = await self._ensure_browser()
        timeout_ms = int(float(getattr(self.settings, "BROWSER_TIMEOUT_SECONDS", 30)) * 1000)
        max_requests = int(getattr(self.settings, "BROWSER_MAX_REQUESTS", 80))
        max_total_bytes = int(getattr(self.settings, "BROWSER_MAX_TOTAL_BYTES", 8_000_000))
        state = {
            "requests": 0,
            "blocked": None,
            "document_final_url": target_url,
            "document_content_type": None,
            "document_status": 0,
        }
        route_client = BrowserRouteHTTPClient(self.settings)

        context = await browser.new_context(
            accept_downloads=False,
            java_script_enabled=True,
            service_workers="block",
        )
        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms)

        async def route_guard(route: Any, request: Any) -> None:
            state["requests"] += 1
            if state["requests"] > max_requests:
                state["blocked"] = "The browser request limit was exceeded"
                await route.abort()
                return
            if request.resource_type in BLOCKED_BROWSER_RESOURCE_TYPES:
                await route.abort()
                return

            try:
                routed = await route_client.fetch(
                    request.url,
                    method=request.method,
                    headers=request.headers,
                )
            except FetchTimeoutError as exc:
                state["blocked"] = str(exc)
                await route.abort()
                return
            except FetchError as exc:
                state["blocked"] = str(exc)
                await route.abort()
                return

            if (
                request.is_navigation_request()
                and request.resource_type == "document"
                and getattr(request, "frame", None) is getattr(page, "main_frame", None)
            ):
                state["document_final_url"] = routed.final_url
                state["document_content_type"] = routed.content_type
                state["document_status"] = routed.status
            await route.fulfill(
                status=routed.status,
                headers=routed.headers,
                body=routed.body,
            )

        async def cancel_download(download: Any) -> None:
            state["blocked"] = "Downloads are not allowed during retrieval"
            await download.cancel()

        def block_websocket(websocket_route: Any) -> None:
            state["blocked"] = "WebSocket requests are not allowed during retrieval"
            result = websocket_route.close()
            if hasattr(result, "__await__"):
                asyncio.create_task(result)

        await context.route("**/*", route_guard)
        if hasattr(context, "route_web_socket"):
            await context.route_web_socket("**/*", block_websocket)
        page = await context.new_page()

        async def close_popup(opened_page: Any) -> None:
            if opened_page is not page:
                await opened_page.close()

        context.on(
            "page",
            lambda opened_page: asyncio.create_task(close_popup(opened_page)),
        )
        page.on("download", lambda download: asyncio.create_task(cancel_download(download)))

        try:
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
            except self._timeout_error:
                pass
            if state["blocked"]:
                raise FetchError(str(state["blocked"]))
            if response is None:
                raise FetchError("The browser did not receive a document response")
            final_url = str(state["document_final_url"] or response.url)
            final_url = await validate_target(final_url, self.settings)
            content_type = str(state["document_content_type"] or response.headers.get("content-type") or "")
            if not content_type_allowed(content_type):
                raise FetchError("The browser retrieved an unsupported content type")
            html = await page.content()
            if len(html.encode("utf-8")) > max_total_bytes:
                raise FetchError("The rendered content exceeded the size limit")
            return RetrievedPage(
                requested_url=target_url,
                final_url=final_url,
                status=int(state["document_status"] or response.status),
                content_type=content_type,
                html=html,
                retrieval_method="browser",
            )
        except self._timeout_error as exc:
            raise FetchTimeoutError("The browser retrieval timed out") from exc
        finally:
            await route_client.close()
            await context.close()
