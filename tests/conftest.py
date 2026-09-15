"""Default tests cannot call websites or paid APIs, even if keys exist locally."""

import httpx
import pytest


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("External HTTP is disabled in tests; inject MockTransport")

    async def deny_async(*args, **kwargs):
        deny()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
