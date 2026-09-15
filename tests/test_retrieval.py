from __future__ import annotations

import gzip
import json
import socket
import ssl

import httpcore
import httpx
import pytest
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream

from app.config import Settings
from app.retrieval.service import (
    BrowserRenderer,
    BrowserRouteHTTPClient,
    FetchError,
    PinnedPublicNetworkBackend,
    RetrievalService,
    RetrievedPage,
    StaticFetcher,
    needs_browser,
    retrieve_structured,
)
from app.retrieval.urls import (
    URLValidationError,
    canonicalise_url,
    domain_key,
    validate_public_url,
)


def settings(**overrides):
    values = {
        "APP_ENV": "test",
        "STATIC_FETCH_WORKER_URL": "http://localhost/worker",
        "STATIC_FETCH_WORKER_SECRET": "secret",
        "PLAYWRIGHT_ENABLED": False,
    }
    values.update(overrides)
    return Settings(**values)


def public_dns(monkeypatch, address="93.184.216.34"):
    def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


class AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class FakeHTTPStream(AsyncNetworkStream):
    def __init__(self):
        self.reads = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok",
            b"",
        ]
        self.tls_hosts = []
        self.writes = []

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self.reads.pop(0)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.append(buffer)

    async def aclose(self) -> None:
        pass

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> AsyncNetworkStream:
        self.tls_hosts.append(server_hostname)
        return self


class FakeNetworkBackend(AsyncNetworkBackend):
    def __init__(self):
        self.connections = []
        self.stream = FakeHTTPStream()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> AsyncNetworkStream:
        self.connections.append((host, port))
        return self.stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options=None,
    ) -> AsyncNetworkStream:
        raise AssertionError("Unix socket access should not be used")

    async def sleep(self, seconds: float) -> None:
        pass


class FakeBrowserResponse:
    url = "https://example.com/"
    status = 200
    headers = {"content-type": "text/html"}


class FakePage:
    def __init__(self, context):
        self.context = context
        self.events = []

    def on(self, event, handler):
        self.events.append((event, handler))

    async def goto(self, url, wait_until=None, timeout=None):
        assert self.context.route_registered_before_page
        assert self.context.websocket_route_registered_before_page
        return FakeBrowserResponse()

    async def wait_for_load_state(self, state, timeout=None):
        raise TimeoutError

    async def content(self):
        return "<main>Ada Lovelace</main>"


class FakeBrowserContext:
    def __init__(self):
        self.page_created = False
        self.route_registered_before_page = False
        self.websocket_route_registered_before_page = False
        self.events = []

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout):
        self.default_navigation_timeout = timeout

    async def route(self, pattern, handler):
        assert not self.page_created
        self.route_registered_before_page = True
        self.route_handler = handler

    async def route_web_socket(self, pattern, handler):
        assert not self.page_created
        self.websocket_route_registered_before_page = True

    async def new_page(self):
        self.page_created = True
        return FakePage(self)

    def on(self, event, handler):
        self.events.append((event, handler))

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.context = FakeBrowserContext()

    async def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self.context


def test_canonicalise_strips_tracking_and_fragment():
    url = "HTTPS://WWW.Example.COM:443/a?q=smith&ref=profile&utm_source=x&fbclid=y#section"
    assert canonicalise_url(url) == "https://www.example.com/a?q=smith&ref=profile"


def test_canonicalise_dedups_root_path_and_rejects_unsafe_urls():
    assert canonicalise_url("https://example.com") == "https://example.com/"
    assert canonicalise_url("https://example.com/") == "https://example.com/"
    with pytest.raises(URLValidationError):
        canonicalise_url("https://example.com\\admin")
    with pytest.raises(URLValidationError):
        canonicalise_url("https://example.com/\nadmin")


def test_domain_key_groups_subdomains():
    assert domain_key("https://profile.news.example.co.uk/path") == "example.co.uk"
    assert domain_key("https://people.example.com") == "example.com"


@pytest.mark.asyncio
async def test_validate_public_url_rejects_private_dns(monkeypatch):
    public_dns(monkeypatch, "10.0.0.5")

    with pytest.raises(URLValidationError):
        await validate_public_url("https://example.com/profile", settings())


@pytest.mark.asyncio
async def test_validate_public_url_accepts_arbitrary_public_sources(monkeypatch):
    public_dns(monkeypatch)
    assert await validate_public_url("https://employer.example/alumni", settings())
    assert await validate_public_url("https://outside.example/profile", settings())


@pytest.mark.asyncio
async def test_pinned_network_backend_connects_ip_and_preserves_tls_hostname(
    monkeypatch,
):
    public_dns(monkeypatch)
    fake_backend = FakeNetworkBackend()
    pinned = PinnedPublicNetworkBackend(settings(), backend=fake_backend)
    pool = httpcore.AsyncConnectionPool(
        network_backend=pinned,
        http1=True,
        http2=False,
        max_connections=1,
        max_keepalive_connections=0,
    )

    response = await pool.handle_async_request(
        httpcore.Request("GET", "https://example.com/", headers={"Host": "example.com"})
    )
    body = await response.aread()
    await pool.aclose()

    assert body == b"ok"
    assert pinned.connections == [("example.com", "93.184.216.34", 443)]
    assert fake_backend.connections == [("93.184.216.34", 443)]
    assert fake_backend.stream.tls_hosts == ["example.com"]


@pytest.mark.asyncio
async def test_pinned_network_backend_rejects_mixed_private_dns(monkeypatch):
    def fake_getaddrinfo(host, port, type=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    fake_backend = FakeNetworkBackend()
    pinned = PinnedPublicNetworkBackend(settings(), backend=fake_backend)

    with pytest.raises(httpcore.ConnectError):
        await pinned.connect_tcp("example.com", 443)
    assert fake_backend.connections == []


@pytest.mark.asyncio
async def test_static_fetcher_posts_to_worker_and_validates_result(monkeypatch):
    public_dns(monkeypatch)
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["secret"] = request.headers.get("x-worker-secret")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "requested_url": "https://example.com/page?utm_source=x",
                "final_url": "https://www.example.com/page",
                "status": 200,
                "content_type": "text/html; charset=utf-8",
                "html": "<html><body>Hello</body></html>",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = StaticFetcher(settings(), client=client)

    page = await fetcher.fetch("https://example.com/page?utm_source=x")

    assert seen["url"] == "http://localhost/worker"
    assert seen["secret"] == "secret"
    assert seen["body"] == {"url": "https://example.com/page"}
    assert page.requested_url == "https://example.com/page"
    assert page.final_url == "https://www.example.com/page"
    assert page.retrieval_method == "static"
    await client.aclose()


@pytest.mark.asyncio
async def test_static_fetcher_rejects_private_final_redirect(monkeypatch):
    public_dns(monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "requested_url": "https://example.com/page",
                "final_url": "http://127.0.0.1/admin",
                "status": 200,
                "content_type": "text/html",
                "html": "private",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = StaticFetcher(settings(), client=client)

    with pytest.raises(FetchError):
        await fetcher.fetch("https://example.com/page")
    await client.aclose()


@pytest.mark.asyncio
async def test_static_fetcher_rejects_oversize_worker_response(monkeypatch):
    public_dns(monkeypatch)
    cfg = settings(MAX_RESPONSE_BYTES=1024)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"html":"' + (b"x" * 2000) + b'"}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = StaticFetcher(cfg, client=client)

    with pytest.raises(FetchError):
        await fetcher.fetch("https://example.com/page")
    await client.aclose()


@pytest.mark.asyncio
async def test_browser_route_client_rejects_chunked_oversize(monkeypatch):
    public_dns(monkeypatch)
    cfg = settings(MAX_RESPONSE_BYTES=1024, BROWSER_MAX_TOTAL_BYTES=4096)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=AsyncChunks([b"x" * 600, b"y" * 600]),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    route_client = BrowserRouteHTTPClient(cfg, client=client)

    with pytest.raises(FetchError):
        await route_client.fetch("https://example.com/page")
    await client.aclose()


@pytest.mark.asyncio
async def test_browser_route_client_validates_redirect_hops(monkeypatch):
    public_dns(monkeypatch)
    cfg = settings()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={
                "location": "http://127.0.0.1/admin",
                "content-type": "text/html",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    route_client = BrowserRouteHTTPClient(cfg, client=client)

    with pytest.raises(FetchError):
        await route_client.fetch("https://example.com/page")
    await client.aclose()


@pytest.mark.asyncio
async def test_browser_route_client_preserves_public_redirect_and_strips_headers(
    monkeypatch,
):
    public_dns(monkeypatch)
    cfg = settings()
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == "https://example.com/start":
            return httpx.Response(
                302,
                headers={"location": "/final", "content-type": "text/html"},
            )
        return httpx.Response(
            200,
            headers={
                "content-encoding": "gzip",
                "content-length": "100",
                "content-type": "text/html",
            },
            content=gzip.compress(b"<main>ok</main>"),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    route_client = BrowserRouteHTTPClient(cfg, client=client)

    redirect = await route_client.fetch("https://example.com/start")
    response = await route_client.fetch(redirect.headers["location"])

    assert seen == ["https://example.com/start", "https://example.com/final"]
    assert redirect.status == 302
    assert redirect.headers["location"] == "https://example.com/final"
    assert redirect.body == b""
    assert response.final_url == "https://example.com/final"
    assert response.body == b"<main>ok</main>"
    assert "content-encoding" not in response.headers
    assert "content-length" not in response.headers
    await client.aclose()


@pytest.mark.asyncio
async def test_browser_route_client_allows_only_get_and_head(monkeypatch):
    public_dns(monkeypatch)
    route_client = BrowserRouteHTTPClient(settings())

    with pytest.raises(FetchError):
        await route_client.fetch("https://example.com/page", method="POST")


def test_needs_browser_only_for_successful_js_shells():
    cfg = settings(PLAYWRIGHT_ENABLED=True, STATIC_MIN_READABLE_CHARS=250)
    shell = RetrievedPage(
        requested_url="https://example.com",
        final_url="https://example.com",
        status=200,
        content_type="text/html",
        html="<div id='root'></div><script></script><script></script>",
    )
    blocked = shell.model_copy(update={"status": 403, "html": "Access denied captcha"})

    assert needs_browser(shell, cfg)
    assert not needs_browser(blocked, cfg)


@pytest.mark.asyncio
async def test_browser_renderer_registers_context_routes_before_page(monkeypatch):
    public_dns(monkeypatch)
    cfg = settings(PLAYWRIGHT_ENABLED=True, BROWSER_TIMEOUT_SECONDS=1)
    renderer = BrowserRenderer(cfg)
    fake_browser = FakeBrowser()

    async def fake_ensure_browser():
        return fake_browser

    renderer._ensure_browser = fake_ensure_browser

    page = await renderer.fetch("https://example.com/")

    assert page.html == "<main>Ada Lovelace</main>"
    assert fake_browser.context_kwargs["accept_downloads"] is False
    assert fake_browser.context_kwargs["service_workers"] == "block"
    assert fake_browser.context.route_registered_before_page
    assert fake_browser.context.websocket_route_registered_before_page


@pytest.mark.asyncio
async def test_iframe_navigation_cannot_replace_main_document_provenance(monkeypatch):
    from types import SimpleNamespace

    public_dns(monkeypatch)
    browser = FakeBrowser()
    renderer = BrowserRenderer(settings())

    async def ensure():
        return browser

    renderer._ensure_browser = ensure

    async def route_fetch(self, url, **kwargs):
        return SimpleNamespace(
            final_url=url,
            status=200,
            content_type="text/html",
            headers={"content-type": "text/html"},
            body=b"public",
        )

    async def fulfill(**kwargs):
        pass

    async def abort():
        raise AssertionError("Public fixture unexpectedly blocked")

    async def goto(self, url, **kwargs):
        self.main_frame = object()
        route = SimpleNamespace(fulfill=fulfill, abort=abort)
        for target, frame in [(url, self.main_frame), ("https://iframe.example.org/", object())]:
            request = SimpleNamespace(
                url=target,
                method="GET",
                headers={},
                frame=frame,
                resource_type="document",
                is_navigation_request=lambda: True,
            )
            await self.context.route_handler(route, request)
        return FakeBrowserResponse()

    monkeypatch.setattr(BrowserRouteHTTPClient, "fetch", route_fetch)
    monkeypatch.setattr(FakePage, "goto", goto)
    result = await renderer.fetch("https://example.com/")
    assert result.final_url == "https://example.com/"
    assert browser.context.closed


@pytest.mark.asyncio
async def test_browser_launch_failure_is_a_source_failure(monkeypatch):
    public_dns(monkeypatch)
    renderer = BrowserRenderer(settings())

    async def broken_launch():
        raise RuntimeError("private runtime details")

    renderer._ensure_browser = broken_launch
    with pytest.raises(FetchError, match="^The browser retrieval failed$"):
        await renderer.fetch("https://example.com/")


@pytest.mark.asyncio
async def test_retrieval_service_uses_cache_and_browser_fallback(monkeypatch):
    public_dns(monkeypatch)
    cfg = settings(PLAYWRIGHT_ENABLED=True)
    calls = {"static": 0, "browser": 0}

    class FakeStatic:
        async def fetch(self, url):
            calls["static"] += 1
            return RetrievedPage(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                html="<div id='root'></div><script></script><script></script>",
            )

        async def close(self):
            pass

    class FakeBrowser:
        async def fetch(self, url):
            calls["browser"] += 1
            return RetrievedPage(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                html="<main>Rendered Ada Lovelace profile</main>",
                retrieval_method="browser",
            )

        async def close(self):
            pass

    service = RetrievalService(cfg, static_fetcher=FakeStatic(), renderer=FakeBrowser(), cache={})

    first = await service.retrieve("https://example.com/profile", job_id="job-1")
    second = await service.retrieve("https://example.com/profile#fragment", job_id="job-1")

    assert first.retrieval_method == "browser"
    assert second.html == first.html
    assert calls == {"static": 1, "browser": 1}


@pytest.mark.asyncio
async def test_retrieval_service_preserves_original_requested_url_after_browser_fallback(
    monkeypatch,
):
    public_dns(monkeypatch)
    cfg = settings(PLAYWRIGHT_ENABLED=True)

    class FakeStatic:
        async def fetch(self, url):
            return RetrievedPage(
                requested_url="https://example.com/a",
                final_url="https://example.com/b",
                status=200,
                content_type="text/html",
                html="<div id='root'></div><script></script><script></script>",
            )

        async def close(self):
            pass

    class FakeBrowser:
        async def fetch(self, url):
            return RetrievedPage(
                requested_url=url,
                final_url="https://example.com/b",
                status=200,
                content_type="text/html",
                html="<main>Rendered</main>",
                retrieval_method="browser",
            )

        async def close(self):
            pass

    service = RetrievalService(cfg, static_fetcher=FakeStatic(), renderer=FakeBrowser())

    page = await service.retrieve("https://example.com/a", job_id="job-1")

    assert page.requested_url == "https://example.com/a"
    assert page.final_url == "https://example.com/b"
    assert page.retrieval_method == "browser"


@pytest.mark.asyncio
async def test_retrieval_service_revalidates_cached_payload_against_current_policy(
    monkeypatch,
):
    public_dns(monkeypatch)
    cache = {
        (
            "job-1",
            "https://employer.example.org/profile",
        ): {
            "requested_url": "https://employer.example.org/profile",
            "final_url": "http://127.0.0.1/profile",
            "status": 200,
            "content_type": "text/html",
            "html": "stale allowed content",
            "retrieval_method": "static",
        }
    }
    cfg = settings()
    calls = {"static": 0}

    class FakeStatic:
        async def fetch(self, url):
            calls["static"] += 1
            return RetrievedPage(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                html="fresh allowed content",
            )

        async def close(self):
            pass

    service = RetrievalService(cfg, static_fetcher=FakeStatic(), renderer=None, cache=cache)

    page = await service.retrieve("https://employer.example.org/profile", job_id="job-1")

    assert page.html == "fresh allowed content"
    assert calls["static"] == 1


@pytest.mark.asyncio
async def test_structured_bulk_filters_records(monkeypatch):
    public_dns(monkeypatch)
    cfg = settings()

    class FakeService:
        settings = cfg

        async def retrieve(self, url, job_id=None):
            return RetrievedPage(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="application/json",
                html="",
                captured_json={
                    "items": [
                        {"name": "Ada Lovelace", "role": "Mathematician"},
                        {"name": "Grace Hopper", "role": "Computer scientist"},
                    ]
                },
            )

    pages = await retrieve_structured(
        {
            "mode": "bulk",
            "url": "https://example.com/directory.json",
            "record_path": "items",
        },
        {"full_name": "Ada Lovelace"},
        FakeService(),
        job_id="job-1",
    )

    assert pages[0].captured_json == {"records": [{"name": "Ada Lovelace", "role": "Mathematician"}]}


@pytest.mark.asyncio
async def test_structured_filtered_expands_seed_params(monkeypatch):
    public_dns(monkeypatch)
    cfg = settings()
    seen = {}

    class FakeService:
        settings = cfg

        async def retrieve(self, url, job_id=None):
            seen["url"] = url
            return RetrievedPage(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="application/json",
                captured_json={"records": []},
            )

    await retrieve_structured(
        {
            "mode": "filtered",
            "url": "https://example.com/search?source=directory",
            "params": {"q": "$full_name"},
        },
        {"full_name": "Ada Lovelace"},
        FakeService(),
    )

    assert seen["url"] == "https://example.com/search?source=directory&q=Ada+Lovelace"
