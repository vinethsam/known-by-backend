from __future__ import annotations

import asyncio
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
    PinnedAsyncHTTPTransport,
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
    is_blocked_source_host,
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
    with pytest.raises(URLValidationError):
        canonicalise_url("https://[broken/profile")


def test_domain_key_groups_subdomains():
    assert domain_key("https://profile.news.example.co.uk/path") == "example.co.uk"
    assert domain_key("https://people.example.com") == "example.com"


@pytest.mark.parametrize(
    "host",
    [
        "linkedin.com",
        "www.linkedin.com",
        "lnkd.in",
        "go.lnkd.in",
        "licdn.com",
        "static.licdn.com",
        "linkedin.cn",
        "people.linkedin.cn",
    ],
)
def test_automated_source_policy_blocks_linkedin_exact_hosts_and_subdomains(host):
    assert is_blocked_source_host(host)


@pytest.mark.parametrize(
    "host",
    ["notlinkedin.com", "linkedin.com.attacker.example", "lnkd.invalid", "example.org"],
)
def test_automated_source_policy_allows_hostname_lookalikes(host):
    assert not is_blocked_source_host(host)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    ["linkedin.com", "www.linkedin.com", "lnkd.in", "licdn.com", "linkedin.cn"],
)
async def test_validate_public_url_blocks_linkedin_before_dns(monkeypatch, host):
    def no_dns(*args):
        raise AssertionError("Blocked source hosts must not be resolved")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)

    with pytest.raises(URLValidationError, match="automated-source policy"):
        await validate_public_url(f"https://{host}/person", settings())


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
async def test_browser_route_rejects_redirect_to_linkedin_before_request(monkeypatch):
    public_dns(monkeypatch)
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://www.linkedin.com/in/jane"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        route_client = BrowserRouteHTTPClient(settings(), client)
        with pytest.raises(FetchError, match="automated-source policy"):
            await route_client.fetch("https://allowed.example/profile")

    assert requests == ["https://allowed.example/profile"]


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


@pytest.mark.asyncio
async def test_retrieval_bounds_overlap_without_domain_starvation(monkeypatch):
    public_dns(monkeypatch)

    async def validated(url, settings):
        return canonicalise_url(url)

    monkeypatch.setattr("app.retrieval.service._validate_target", validated)
    release = asyncio.Event()
    overlap = asyncio.Event()
    active = set()
    peaks = {"global": 0, "domain": 0}

    class Fetcher:
        async def fetch(self, url):
            active.add(url)
            peaks["global"] = max(peaks["global"], len(active))
            domain_count = sum(domain_key(item) == domain_key(url) for item in active)
            peaks["domain"] = max(peaks["domain"], domain_count)
            if len(active) == 2:
                overlap.set()
            try:
                await release.wait()
                return RetrievedPage(requested_url=url, final_url=url, status=200, html="public")
            finally:
                active.remove(url)

        async def close(self):
            pass

    service = RetrievalService(settings(MAX_CONCURRENT_FETCHES=2, PER_DOMAIN_CONCURRENCY=1), Fetcher())
    tasks = [
        asyncio.create_task(service.retrieve(url, job_id="job"))
        for url in [
            "https://a.example.com/first",
            "https://b.example.com/second",
            "https://independent.org/third",
            "https://another.net/fourth",
        ]
    ]
    try:
        await asyncio.wait_for(overlap.wait(), timeout=2)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert peaks == {"global": 2, "domain": 1}
    assert not service._locks
    assert not service._domain_sems


@pytest.mark.asyncio
async def test_concurrent_same_job_cache_reuse_and_job_isolation(monkeypatch):
    public_dns(monkeypatch)
    calls = []

    class Fetcher:
        async def fetch(self, url):
            calls.append(url)
            await asyncio.sleep(0)
            return RetrievedPage(requested_url=url, final_url=url, status=200, html="public")

        async def close(self):
            pass

    service = RetrievalService(settings(), Fetcher(), cache={})
    first, second = await asyncio.gather(
        service.retrieve("https://example.com/profile", job_id="one"),
        service.retrieve("https://example.com/profile?utm_source=x", job_id="one"),
    )
    assert first.html == second.html
    assert len(calls) == 1
    await service.retrieve("https://example.com/profile", job_id="two")
    assert len(calls) == 2
    assert not service._locks


@pytest.mark.asyncio
async def test_retrieval_cancellation_releases_pending_limits(monkeypatch):
    public_dns(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    class Fetcher:
        async def fetch(self, url):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return RetrievedPage(requested_url=url, final_url=url, status=200, html="public")

        async def close(self):
            pass

    service = RetrievalService(
        settings(MAX_CONCURRENT_FETCHES=1, PER_DOMAIN_CONCURRENCY=1), Fetcher(), cache={}
    )
    first = asyncio.create_task(service.retrieve("https://example.com/a", job_id="job"))
    await started.wait()
    pending = asyncio.create_task(service.retrieve("https://example.com/b", job_id="job"))
    same_url = asyncio.create_task(service.retrieve("https://example.com/a", job_id="job"))
    await asyncio.sleep(0)
    for task in (first, pending, same_url):
        task.cancel()
    outcomes = await asyncio.gather(first, pending, same_url, return_exceptions=True)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert cancelled.is_set()
    assert not service._locks
    assert not service._domain_sems
    release.set()
    assert (await service.retrieve("https://example.com/c", job_id="job")).html == "public"


@pytest.mark.asyncio
async def test_static_fetcher_reuses_owned_client_and_closes_it():
    fetcher = StaticFetcher(settings(MAX_CONCURRENT_FETCHES=3))
    first = await fetcher._client_for_request()
    assert await fetcher._client_for_request() is first
    await fetcher.close()
    assert first.is_closed
    assert fetcher._client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_setup", [False, True])
async def test_browser_closes_context_and_route_client_when_setup_fails(monkeypatch, cancel_setup):
    public_dns(monkeypatch)
    browser = FakeBrowser()
    renderer = BrowserRenderer(settings())
    setup_started = asyncio.Event()
    closed_clients = []

    async def ensure():
        return browser

    async def broken_route(*args):
        setup_started.set()
        if cancel_setup:
            await asyncio.Event().wait()
        raise RuntimeError("private setup failure")

    async def close_client(self):
        closed_clients.append(self)

    renderer._ensure_browser = ensure
    browser.context.route = broken_route
    monkeypatch.setattr(BrowserRouteHTTPClient, "close", close_client)
    task = asyncio.create_task(renderer.fetch("https://example.com/"))
    await setup_started.wait()
    if cancel_setup:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(FetchError, match="^The browser retrieval failed$"):
            await task
    assert browser.context.closed
    assert len(closed_clients) == 1


@pytest.mark.asyncio
async def test_browser_reuses_process_with_isolated_contexts(monkeypatch):
    public_dns(monkeypatch)
    contexts = []

    class Browser:
        async def new_context(self, **kwargs):
            context = FakeBrowserContext()
            contexts.append(context)
            return context

        def is_connected(self):
            return True

    renderer = BrowserRenderer(settings())
    renderer._browser = Browser()
    await asyncio.gather(renderer.fetch("https://example.com/a"), renderer.fetch("https://example.com/b"))
    assert len(contexts) == 2
    assert contexts[0] is not contexts[1]
    assert all(context.closed for context in contexts)


@pytest.mark.asyncio
async def test_disconnected_browser_is_restarted(monkeypatch):
    import playwright.async_api

    events = []

    class Browser:
        def __init__(self, connected):
            self.connected = connected

        def is_connected(self):
            return self.connected

        async def close(self):
            events.append("browser_closed")

    replacement = Browser(True)

    class Runtime:
        @property
        def chromium(self):
            return self

        async def start(self):
            events.append("started")
            return self

        async def launch(self, **kwargs):
            assert kwargs["chromium_sandbox"] is True
            events.append("launched")
            return replacement

        async def stop(self):
            events.append("stopped")

    monkeypatch.setattr(playwright.async_api, "async_playwright", Runtime)
    renderer = BrowserRenderer(settings())
    renderer._browser = Browser(False)
    renderer._playwright = Runtime()
    assert await renderer._ensure_browser() is replacement
    assert await renderer._ensure_browser() is replacement
    assert events == ["browser_closed", "stopped", "started", "launched"]
    await renderer.close()


@pytest.mark.asyncio
async def test_browser_route_reuses_pinned_connection_and_revalidates_dns(monkeypatch):
    public_dns(monkeypatch)
    cfg = settings()
    backend = FakeNetworkBackend()
    backend.stream.reads = [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/html\r\n\r\nok"] * 2
    transport = PinnedAsyncHTTPTransport(cfg)
    transport.network_backend.backend = backend

    async def handler(request):
        response = await transport._pool.handle_async_request(
            httpcore.Request(request.method, str(request.url), headers=request.headers.raw)
        )
        try:
            return httpx.Response(response.status, headers=response.headers, content=await response.aread())
        finally:
            await response.aclose()

    async with transport, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        route_client = BrowserRouteHTTPClient(cfg, client)
        assert (await route_client.fetch("https://example.com/a")).body == b"ok"
        assert (await route_client.fetch("https://example.com/b")).body == b"ok"
        assert backend.connections == [("93.184.216.34", 443)]
        public_dns(monkeypatch, "10.0.0.5")
        with pytest.raises(FetchError):
            await route_client.fetch("https://example.com/c")
        assert len(backend.connections) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("content_type", "html"), [("application/pdf", "old"), ("text/html", "x" * 2000)])
async def test_cache_rechecks_current_content_limits(monkeypatch, content_type, html):
    public_dns(monkeypatch)
    url = "https://example.com/profile"
    cache = {
        ("job", url): RetrievedPage(
            requested_url=url, final_url=url, status=200, content_type=content_type, html=html
        )
    }

    class Fetcher:
        async def fetch(self, url):
            return RetrievedPage(requested_url=url, final_url=url, status=200, html="fresh")

        async def close(self):
            pass

    service = RetrievalService(settings(MAX_RESPONSE_BYTES=1024), Fetcher(), cache=cache)
    assert (await service.retrieve(url, "job")).html == "fresh"


@pytest.mark.asyncio
async def test_browser_cancellation_drains_callback_tasks(monkeypatch):
    from types import SimpleNamespace

    public_dns(monkeypatch)
    browser = FakeBrowser()
    renderer = BrowserRenderer(settings())
    callback_started = asyncio.Event()
    callback_closed = asyncio.Event()

    async def ensure():
        return browser

    async def cancel_download():
        callback_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            callback_closed.set()

    async def goto(self, *args, **kwargs):
        handler = next(handler for event, handler in self.events if event == "download")
        handler(SimpleNamespace(cancel=cancel_download))
        await asyncio.Event().wait()

    renderer._ensure_browser = ensure
    monkeypatch.setattr(FakePage, "goto", goto)
    task = asyncio.create_task(renderer.fetch("https://example.com/"))
    await callback_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert browser.context.closed
    assert callback_closed.is_set()
