from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.providers.openrouter import (
    OPENROUTER_CHAT_COMPLETIONS_URL,
    WEB_SEARCH_MAX_CHARACTERS,
    WEB_SEARCH_MAX_OUTPUT_TOKENS,
    OpenRouterClient,
)
from app.providers.search import OpenRouterSearchProvider, SearchProviderError
from app.research.budget import BudgetedModel, BudgetExceeded
from app.schemas import ResearchMetrics

CONTEXT = {"job_id": "job-1", "person_id": "person-1"}


def settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        OPENROUTER_API_KEY="openrouter-token",
        OPENROUTER_SOURCE_MODEL="source-model",
        OPENROUTER_EXTRACTION_MODEL="extraction-model",
        **{"OPENROUTER_MAX_RETRIES": 1, **overrides},
    )


async def no_sleep(_: float) -> None:
    pass


@pytest.mark.parametrize("role", ["OPENROUTER_SOURCE_MODEL", "OPENROUTER_EXTRACTION_MODEL"])
@pytest.mark.parametrize(
    "model", ["vendor/model:online", "  vendor/model:ONLINE  ", "@preset", "  @preset  "]
)
def test_model_settings_reject_implicit_search_and_presets_in_both_roles(role, model):
    with pytest.raises(ValidationError, match="Use a plain model ID"):
        Settings(_env_file=None, **{role: model})


def citation(url="https://example.org/jane", title="Jane Doe", content="Jane is a fellow."):
    return {"type": "url_citation", "url_citation": {"url": url, "title": title, "content": content}}


def response(annotations=None, *, usage=None, content="Found a source."):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content, "annotations": annotations}}],
            "usage": usage
            if usage is not None
            else {
                "input_tokens": 21,
                "output_tokens": 13,
                "cost": 0.007,
                "server_tool_use": {"web_search_requests": 1},
            },
        },
    )


@pytest.mark.asyncio
async def test_shared_openrouter_search_uses_hard_caps_and_citation_metadata():
    requests, before, usage = [], [], []

    def handler(request):
        requests.append(request)
        return response([citation()])

    config = settings(MAX_SEARCH_RESULTS_PER_QUERY=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(config, http)
        provider = OpenRouterSearchProvider(config, client)
        results = await provider.search(
            " Jane   Doe fellowship ",
            100,
            context=CONTEXT,
            before_attempt=before.append,
            on_usage=usage.append,
        )
        assert provider.client.client is http
    request = requests[0]
    body = json.loads(request.content)
    assert str(request.url) == OPENROUTER_CHAT_COMPLETIONS_URL
    assert request.headers["authorization"] == "Bearer openrouter-token"
    assert body["model"] == "source-model"
    assert body["provider"] == {"require_parameters": True}
    assert body["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {
                "engine": "exa",
                "max_results": 3,
                "max_total_results": 3,
                "max_uses": 1,
                "max_characters": WEB_SEARCH_MAX_CHARACTERS,
            },
        }
    ]
    assert body["max_tool_calls"] == 1
    assert body["max_tokens"] == WEB_SEARCH_MAX_OUTPUT_TOKENS
    assert "response_format" not in body and "plugins" not in body
    assert json.loads(body["messages"][1]["content"])["query"] == "Jane Doe fellowship"
    assert len(before) == len(usage) == 1
    assert before[0]["prompt_version"] == "web-discovery-v1"
    assert usage[0].prompt_tokens == 21 and usage[0].completion_tokens == 13
    assert usage[0].web_search_requests == 1 and usage[0].cost == 0.007
    assert usage[0].role == "source" and usage[0].success
    assert results[0].url == "https://example.org/jane"
    assert results[0].title == "Jane Doe" and results[0].snippet == "Jane is a fellow."
    assert results[0].domain == "example.org" and results[0].origin == "openrouter_web_search"


@pytest.mark.asyncio
async def test_generated_prose_json_and_tool_arguments_are_never_source_urls():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"url":"https://invented.example/profile"}',
                            "tool_calls": [
                                {"function": {"name": "fetch", "arguments": '{"url":"https://tool.example"}'}}
                            ],
                            "annotations": [{"type": "other", "url": "https://other.example"}],
                        }
                    }
                ]
            },
        )

    config = settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert (
            await OpenRouterSearchProvider(config, OpenRouterClient(config, http)).search(
                "Jane Doe", 2, context=CONTEXT
            )
            == []
        )
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("annotations", [None, [], [{"type": "url_citation", "url_citation": None}]])
async def test_missing_citations_are_a_valid_empty_result_without_retries(annotations):
    records = []
    config = settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(annotations))) as http:
        results = await OpenRouterSearchProvider(config, OpenRouterClient(config, http)).search(
            "Jane Doe", 2, context=CONTEXT, on_usage=records.append
        )
    assert results == []
    assert len(records) == 1 and records[0].success


def test_citations_canonicalise_deduplicate_filter_and_bound_results_without_dns(monkeypatch):
    def no_dns(*args):
        raise AssertionError("Discovery must not resolve citation domains")

    monkeypatch.setattr("socket.getaddrinfo", no_dns)
    unsafe_urls = [
        "ftp://example.org/file",
        "example.org/bare",
        "https://[broken",
        "https://user:secret@example.org/a",
        "https://example.org\\@evil.example",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "https://10.1.1.1/a",
        "http://169.254.169.254/a",
        "https://localhost/a",
        "https://metadata.google.internal/a",
    ]
    data = {
        "choices": [
            {
                "message": {
                    "annotations": [
                        None,
                        {"type": "url_citation", "url_citation": {"url": 123}},
                        *[citation(url) for url in unsafe_urls],
                        citation("https://EXAMPLE.org:443/jane?utm_source=x#part", "x" * 500, "s" * 900),
                        citation("https://example.org/jane"),
                    ]
                }
            },
            {"message": {"annotations": [citation("https://other.example/jane", {}, [])]}},
            {"message": {"annotations": [citation("https://third.example/jane")]}},
        ]
    }
    results = OpenRouterSearchProvider._parse_results(data, 2)
    assert [r.url for r in results] == ["https://example.org/jane", "https://other.example/jane"]
    assert len(results[0].title) == 300 and len(results[0].snippet) == 700
    assert results[1].title == results[1].snippet == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [[], {}, {"choices": []}, {"choices": [None]}, {"choices": [{"message": {"annotations": {}}}]}],
)
async def test_malformed_search_envelopes_record_each_paid_attempt(payload):
    before, records = [], []
    config = settings()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        provider = OpenRouterSearchProvider(config, OpenRouterClient(config, http, sleep=no_sleep))
        with pytest.raises(SearchProviderError, match="failed after bounded retries") as error:
            await provider.search(
                "Jane Doe", 2, context=CONTEXT, before_attempt=before.append, on_usage=records.append
            )
    assert len(before) == len(records) == len(error.value.usage) == 2
    assert all(not record.success for record in records)
    assert all(record.web_search_requests is None for record in records)


@pytest.mark.asyncio
async def test_search_retry_and_usage_callbacks_run_once_per_http_attempt():
    calls, before, local, global_records = [], [], [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "provider detail", "usage": {"input_tokens": 2}})
        return response([citation()])

    config = settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(config, http, sleep=no_sleep, usage_callback=global_records.append)
        results = await OpenRouterSearchProvider(config, client).search(
            "Jane Doe", 2, context=CONTEXT, before_attempt=before.append, on_usage=local.append
        )
    assert len(results) == 1
    assert [u.success for u in local] == [False, True]
    assert [u.prompt_tokens for u in local] == [2, 21]
    assert [u.web_search_requests for u in local] == [None, 1]
    assert local == global_records and len(before) == len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_nonretryable_search_errors_are_safe_even_when_not_json(status):
    calls, records = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private provider details and token")

    config = settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SearchProviderError, match=f"^OpenRouter HTTP {status}$") as error:
            await OpenRouterSearchProvider(config, OpenRouterClient(config, http, sleep=no_sleep)).search(
                "Jane Doe", 2, context=CONTEXT, on_usage=records.append
            )
    assert len(calls) == len(records) == len(error.value.usage) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [None, "1", True, -1, 1.5, {}, 0, 1])
async def test_unknown_tool_usage_stays_unknown_and_valid_zero_is_preserved(count):
    config = settings()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: response([], usage={"server_tool_use": {"web_search_requests": count}})
        )
    ) as http:
        _, records = await OpenRouterClient(config, http).web_search("Jane Doe", 2, CONTEXT)
    expected = count if type(count) is int and count >= 0 else None
    assert records[0].web_search_requests == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("primary,expected", [(None, 4), ("bad", 4), (0, 0), (3, 3)])
async def test_token_usage_fallback_preserves_valid_primary_counts(primary, expected):
    config = settings()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: response(
                [],
                usage={
                    "prompt_tokens": primary,
                    "completion_tokens": primary,
                    "input_tokens": 4,
                    "output_tokens": 4,
                },
            )
        )
    ) as http:
        _, records = await OpenRouterClient(config, http).web_search("Jane Doe", 2, CONTEXT)
    assert records[0].prompt_tokens == records[0].completion_tokens == expected


@pytest.mark.asyncio
async def test_retry_reservation_can_stop_transport_without_erasing_first_attempt():
    calls, records, reservations = [], [], []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private detail", request=request)

    async def reserve(attempt):
        reservations.append(attempt)
        if len(reservations) > 1:
            raise RuntimeError("budget exhausted")

    config = settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await OpenRouterSearchProvider(config, OpenRouterClient(config, http, sleep=no_sleep)).search(
                "Jane Doe", 2, context=CONTEXT, before_attempt=reserve, on_usage=records.append
            )
    assert len(reservations) == 2 and len(calls) == len(records) == 1
    assert records[0].web_search_requests is None and not records[0].success


@pytest.mark.asyncio
async def test_oversized_search_stream_is_closed_and_accounted_for():
    class HugeStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"x" * 1025
            raise AssertionError("Do not continue consuming an oversized response")

        async def aclose(self):
            self.closed = True

    stream, records = HugeStream(), []
    config = settings(MAX_RESPONSE_BYTES=1024, OPENROUTER_MAX_RETRIES=0)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as http:
        with pytest.raises(SearchProviderError):
            await OpenRouterSearchProvider(config, OpenRouterClient(config, http)).search(
                "Jane Doe", 2, context=CONTEXT, on_usage=records.append
            )
    assert stream.closed and len(records) == 1 and not records[0].success


@pytest.mark.asyncio
async def test_empty_query_and_zero_limit_never_make_paid_requests():
    def handler(_):
        raise AssertionError("No HTTP request expected")

    config = settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenRouterSearchProvider(config, OpenRouterClient(config, http))
        assert await provider.search("  ", 2, context=CONTEXT) == []
        assert await provider.search("Jane Doe", 0, context=CONTEXT) == []


@pytest.mark.asyncio
async def test_reported_tool_overrun_stops_without_retry_and_preserves_usage():
    calls, ledger, checkpoints = [], [], []
    metrics = ResearchMetrics()

    def handler(request):
        calls.append(request)
        return response(
            [citation()],
            usage={
                "input_tokens": 21,
                "output_tokens": 13,
                "server_tool_use": {"web_search_requests": 2},
            },
        )

    async def checkpoint():
        checkpoints.append((metrics.web_search_calls_reserved, metrics.web_search_requests, len(ledger)))

    config = settings(OPENROUTER_MAX_RETRIES=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient(config, http, sleep=no_sleep)
        budgeted = BudgetedModel(client, config, metrics, ledger)
        budgeted.checkpoint = checkpoint
        with pytest.raises(BudgetExceeded, match="SEARCH_TOOL_LIMIT_VIOLATION"):
            await budgeted.search(OpenRouterSearchProvider(config, client), "Jane Doe", 1, CONTEXT)

    assert len(calls) == len(ledger) == metrics.llm_calls == 1
    assert ledger[0].web_search_requests == metrics.web_search_requests == 2
    assert metrics.web_search_calls_reserved == metrics.search_results_reserved == 1
    assert metrics.tokens_used == metrics.tokens_budgeted == 34
    assert checkpoints == [(1, 0, 0), (1, 2, 1)]
