"""Production concurrency/budget paths with offline HTTP, never paid requests."""

import asyncio
import json
import logging
from contextlib import aclosing
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.logging import JsonFormatter
from app.prompts.extraction import EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT
from app.providers.openrouter import OpenRouterClient
from app.providers.search import OpenRouterSearchProvider
from app.research.budget import BudgetedModel, BudgetExceeded
from app.research.concurrency import ordered_window
from app.research.extraction import extract_chunks
from app.research.orchestrator import ResearchOrchestrator, _process_source
from app.research.telemetry import COUNTERS, TIMINGS, count, person_performance, stage
from app.retrieval.service import RetrievalService, RetrievedPage, StaticFetcher
from app.schemas import ExtractionResponse, PersonSeed, ResearchMetrics, SourceCandidate, SourceRecord
from app.worker import process_lease

CONTEXT = {"job_id": "fixture-job", "person_id": "fixture-person"}


def configured(**changes):
    return Settings(
        _env_file=None,
        OPENROUTER_API_KEY="fixture-key",
        OPENROUTER_SOURCE_MODEL="fixture-source",
        OPENROUTER_EXTRACTION_MODEL="fixture-extraction",
        **changes,
    )


def response(content='{"claims":[]}'):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        },
    )


@pytest.mark.asyncio
async def test_ordered_window_is_bounded_isolates_errors_and_drains_on_exit():
    started, finished = [], []
    ready = asyncio.Event()

    async def operation(index):
        started.append(index)
        try:
            if index == 0:
                await ready.wait()
                raise ValueError("bad source")
            if index == 1:
                ready.set()
                return index
            await asyncio.Event().wait()
        finally:
            finished.append(index)

    async with asyncio.timeout(2):
        async with ordered_window(list(range(100)), operation, 2) as results:
            async for index, outcome in results:
                if index == 0:
                    with pytest.raises(ValueError):
                        outcome.unwrap()
                else:
                    assert outcome.unwrap() == 1
                    await asyncio.sleep(0)
                    break
    assert started == [0, 1, 2]
    assert sorted(finished) == started


@pytest.mark.asyncio
async def test_extraction_out_of_order_is_bounded_and_consumed_in_chunk_order():
    settings = configured(OPENROUTER_MAX_RETRIES=0)
    metrics, usage = ResearchMetrics(), []
    active = peak = 0
    finished = []
    second = asyncio.Event()

    async def handler(request):
        nonlocal active, peak
        index = json.loads(request.content)["messages"][1]["content"]
        index = json.loads(index)["payload"]["chunk_index"]
        active += 1
        peak = max(peak, active)
        try:
            if index == 0:
                await second.wait()
            else:
                second.set()
            finished.append(index)
            return response()
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BudgetedModel(OpenRouterClient(settings, http), settings, metrics, usage)
        checkpoints = []

        async def checkpoint():
            checkpoints.append((metrics.llm_calls, len(usage), metrics.tokens_budgeted))
            await asyncio.sleep(0)

        client.checkpoint = checkpoint
        async with asyncio.timeout(2):
            async with aclosing(
                extract_chunks(
                    client,
                    PersonSeed(full_name="Jane Doe"),
                    ["first", "second", "third"],
                    "https://example.org/jane",
                    CONTEXT,
                    asyncio.Semaphore(2),
                    2,
                )
            ) as chunks:
                output = [chunk async for chunk, _ in chunks]
    assert output == ["first", "second", "third"]
    assert finished == [1, 0, 2]
    assert peak == 2 and active == 0
    assert len(usage) == metrics.llm_calls == 3
    assert metrics.tokens_used == metrics.tokens_budgeted == 90
    assert len(checkpoints) == 6
    assert all(calls <= settings.MAX_LLM_CALLS_PER_PERSON for calls, _, _ in checkpoints)


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", ["calls", "tokens"])
async def test_parallel_attempts_and_retries_cannot_overspend(cap):
    changes = {"OPENROUTER_MAX_RETRIES": 1}
    if cap == "calls":
        changes["MAX_LLM_CALLS_PER_PERSON"] = 2
    settings = configured(**changes)
    metrics, usage = ResearchMetrics(), []
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        # Unknown usage retains the full reservation through failed retries.
        return httpx.Response(503, json={"error": {"message": "fixture failure"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BudgetedModel(
            OpenRouterClient(settings, http, sleep=lambda _: asyncio.sleep(0)), settings, metrics, usage
        )
        reserve = client._reservation(ExtractionResponse, EXTRACTION_SYSTEM_PROMPT, {}, CONTEXT)
        if cap == "tokens":
            settings.MAX_TOKENS_PER_PERSON = reserve * 2

        async def complete():
            return await client.complete(
                ExtractionResponse,
                "extraction",
                EXTRACTION_SYSTEM_PROMPT,
                {},
                CONTEXT,
                EXTRACTION_PROMPT_VERSION,
            )

        outcomes = await asyncio.gather(*(complete() for _ in range(8)), return_exceptions=True)
    assert requests == metrics.llm_calls == len(usage) == 2
    assert all(isinstance(outcome, BudgetExceeded) for outcome in outcomes)
    assert metrics.tokens_budgeted == reserve * 2
    assert metrics.tokens_budgeted <= settings.MAX_TOKENS_PER_PERSON


@pytest.mark.asyncio
async def test_tight_extraction_budget_keeps_serial_priority():
    settings = configured(MAX_LLM_CALLS_PER_PERSON=2, OPENROUTER_MAX_RETRIES=0)
    metrics, usage, indexes = ResearchMetrics(), [], []

    async def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])["payload"]
        indexes.append(payload["chunk_index"])
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BudgetedModel(OpenRouterClient(settings, http), settings, metrics, usage)
        consumed = []
        with pytest.raises(BudgetExceeded, match="MAX_LLM_CALLS"):
            async with aclosing(
                extract_chunks(
                    client,
                    PersonSeed(full_name="Jane Doe"),
                    ["first", "second", "third"],
                    "https://example.org/jane",
                    CONTEXT,
                    asyncio.Semaphore(2),
                    2,
                )
            ) as chunks:
                async for chunk, _ in chunks:
                    consumed.append(chunk)
    assert indexes == [0, 1]
    assert consumed == ["first", "second"]


@pytest.mark.asyncio
async def test_cancel_extractions_drains_inflight_and_never_starts_pending_chunks():
    settings = configured(OPENROUTER_MAX_RETRIES=0)
    metrics, usage = ResearchMetrics(), []
    started, finished = [], []
    ready = asyncio.Event()

    async def handler(request):
        index = json.loads(json.loads(request.content)["messages"][1]["content"])["payload"]["chunk_index"]
        started.append(index)
        if len(started) == 2:
            ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(index)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BudgetedModel(OpenRouterClient(settings, http), settings, metrics, usage)

        async def consume():
            async with aclosing(
                extract_chunks(
                    client,
                    PersonSeed(full_name="Jane Doe"),
                    ["first", "second", "third"],
                    "https://example.org/jane",
                    CONTEXT,
                    asyncio.Semaphore(2),
                    2,
                )
            ) as chunks:
                return [chunk async for chunk in chunks]

        task = asyncio.create_task(consume())
        async with asyncio.timeout(2):
            await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert started == [0, 1] and sorted(finished) == [0, 1]
    assert len(usage) == metrics.llm_calls == 2


@pytest.mark.asyncio
async def test_person_telemetry_isolated_nested_and_safe(caplog):
    caplog.set_level(logging.INFO)

    async def person(index):
        with person_performance("job", str(index), queue_wait_ms=index):
            with person_performance("job", str(index)):
                with stage("retrieval_ms"):
                    await asyncio.sleep(0)
                    count("static_fetches", index)
                    count("provider_cost", 0.0018)

    await asyncio.gather(person(1), person(2))
    records = [record for record in caplog.records if record.msg == "person_performance"]
    assert len(records) == 2
    for record in records:
        event = json.loads(JsonFormatter().format(record))
        performance = event["performance"]
        assert set(performance) == set(TIMINGS + COUNTERS)
        assert performance["queue_wait_ms"] == performance["static_fetches"] == int(record.person_id)
        assert performance["total_person_ms"] >= performance["retrieval_ms"] >= 0
        assert performance["provider_cost"] == 0.0018
        assert "fixture-key" not in json.dumps(event)


@pytest.mark.asyncio
async def test_owned_openrouter_client_pool_reused_and_closed():
    provider = OpenRouterClient(configured())
    first = provider.client
    assert provider.client is first
    assert not first.is_closed
    await provider.aclose()
    assert first.is_closed


def test_extraction_concurrency_setting_is_conservative_and_bounded():
    assert configured().MAX_CONCURRENT_EXTRACTIONS == 2
    for value in [0, 21]:
        with pytest.raises(ValidationError):
            configured(MAX_CONCURRENT_EXTRACTIONS=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,code", [(RuntimeError, "RESEARCH_FAILED"), (TimeoutError, "PERSON_TIMEOUT")])
async def test_lease_renewal_failure_cancels_owned_research_before_terminal_write(error, code):
    started, cancelled = asyncio.Event(), asyncio.Event()
    failures = []

    class Pipeline:
        async def research(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class Store:
        def renew_lease(self, lease):
            assert started.is_set()
            raise error("fixture database unavailable")

        def fail_task(self, lease, code):
            assert cancelled.is_set()
            failures.append(code)

    lease = SimpleNamespace(job_id="job", person_id="person", seed=PersonSeed(full_name="Jane Doe"))
    async with asyncio.timeout(2):
        await process_lease(Store(), Pipeline(), lease, configured(WORKER_HEARTBEAT_SECONDS=0.05))
    assert cancelled.is_set()
    assert failures == [code]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_source,redirect_source,later_preferred",
    [(False, False, False), (True, False, False), (False, True, False), (False, True, True)],
)
async def test_production_prefetch_overlaps_but_evidence_and_errors_keep_source_order(
    monkeypatch, failed_source, redirect_source, later_preferred
):
    async def safe(url, settings=None):
        return url

    monkeypatch.setattr("app.retrieval.service.validate_public_url", safe)
    urls = ["https://employer.example.org/jane", "https://other.example.net/jane"]
    later_url = "https://third.example.com/jane"
    seed = PersonSeed(
        full_name="Jane Doe",
        organisation="Example Foundation",
        preferred_urls=urls + ([later_url] if later_preferred else []),
    )

    async def run(width):
        settings = configured(
            MAX_SOURCES_PER_PERSON=2,
            MAX_CONCURRENT_FETCHES=width,
            OPENROUTER_MAX_RETRIES=0,
            PLAYWRIGHT_ENABLED=False,
            STATIC_FETCH_WORKER_URL="https://worker.example.org/fetch",
            STATIC_FETCH_WORKER_SECRET="fixture-secret",
        )
        second_started = asyncio.Event()
        fetched, extracted = [], []
        statement = "Jane Doe works at Example Foundation."

        async def fetch(request):
            url = json.loads(request.content)["url"]
            fetched.append(url)
            if width > 1 and url == urls[0]:
                await second_started.wait()
            if url == urls[1]:
                second_started.set()
            if failed_source and url == urls[0]:
                return httpx.Response(502, json={"error": "offline fixture"})
            return httpx.Response(
                200,
                json={
                    "requested_url": url,
                    "final_url": urls[1] if redirect_source and url == urls[0] else url,
                    "status": 200,
                    "content_type": "text/html",
                    "html": f"<h1>Jane Doe</h1><p>{statement}</p><p>{url}</p>",
                },
            )

        async def model(request):
            body = json.loads(request.content)
            if body.get("tools"):
                assert redirect_source, "Source cap should prevent further discovery"
                assert not later_preferred, "Later selected evidence must survive a skipped redirect alias"
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url_citation": {"url": later_url, "title": "Jane Doe"},
                                        }
                                    ],
                                }
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                    },
                )
            payload = json.loads(body["messages"][1]["content"])["payload"]
            if payload.get("task") == "validate_candidates":
                answer = {
                    "decisions": [
                        {"candidate_id": c["candidate_id"], "source_type": "employer", "relevance": "likely"}
                        for c in payload["candidates"]
                    ]
                }
            else:
                extracted.append(payload["source_url"])
                if width > 1:
                    assert fetched[:2] == urls
                answer = {
                    "claims": [
                        {
                            "field": "full_name",
                            "value": "Jane Doe",
                            "evidence": statement,
                            "subject_name": "Jane Doe",
                        }
                    ]
                }
            return response(json.dumps(answer))

        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
            async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as fetch_http:
                provider = OpenRouterClient(settings, model_http)
                retrieval = RetrievalService(settings, StaticFetcher(settings, fetch_http))
                orchestrator = ResearchOrchestrator(
                    settings, OpenRouterSearchProvider(settings, provider), provider, retrieval
                )
                snapshots = []

                async def checkpoint(result):
                    snapshots.append((result, result.model_dump(mode="json")))
                    await asyncio.sleep(0)

                async with asyncio.timeout(3):
                    result = await orchestrator.research("job", "person", seed, checkpoint)
                await retrieval.close()
        assert all(saved.model_dump(mode="json") == frozen for saved, frozen in snapshots)
        expected = [urls[1], later_url] if redirect_source else urls[1:] if failed_source else urls
        assert extracted == expected
        if redirect_source:
            assert fetched == [*urls, later_url] if width > 1 else fetched == [urls[0], later_url]
            assert len(fetched) <= 2 * settings.MAX_SOURCES_PER_PERSON - 1
        return {
            "fields": {
                key.value: (field.value, field.confidence, field.sources, field.review_reason_codes)
                for key, field in result.profile.fields.items()
            },
            "sources": [
                (source.final_url, source.processing_status, source.error_code) for source in result.sources
            ],
            "claims": [(claim.field, claim.raw_value, claim.evidence_text) for claim in result.claims],
            "coverage": result.profile.coverage,
            "stop": result.profile.metrics.stop_reason,
            "errors": result.profile.metrics.error_codes,
        }

    assert await run(2) == await run(1)


def test_distinct_captured_json_pages_are_not_false_empty_body_duplicates():
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    candidate = SourceCandidate(url="https://example.org/jane", source_type="employer", relevance="likely")
    hashes, texts = [], []
    payloads = [
        {"name": "Jane Doe", "organisation": "Example Foundation"},
        {"name": "Jane Doe", "education": "Example University"},
        {"organisation": "Example Foundation", "name": "Jane Doe"},
    ]
    for payload in payloads:
        page = RetrievedPage(
            requested_url=candidate.url,
            final_url=candidate.url,
            status=200,
            content_type="application/json",
            captured_json=payload,
        )
        source = SourceRecord(
            person_id="person",
            requested_url=candidate.url,
            final_url=candidate.url,
            canonical_url=candidate.url,
            domain="example.org",
        )
        texts.append(_process_source(source, candidate, page, seed, configured()))
        hashes.append(source.content_hash)
    assert hashes[0] != hashes[1]
    assert hashes[0] == hashes[2]
    assert "Example Foundation" in "".join(texts[0])
    assert "Example University" in "".join(texts[1])
