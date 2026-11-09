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
from app.providers.openrouter import OpenRouterClient, OpenRouterError
from app.providers.search import OpenRouterSearchProvider
from app.research.budget import (
    DEEPSEEK_EXTRACTION_MODEL,
    LUNA_EXTRACTION_FALLBACK,
    BudgetedModel,
    BudgetExceeded,
)
from app.research.concurrency import ordered_window
from app.research.extraction import extract_chunks, extraction_chunk_key
from app.research.orchestrator import ResearchOrchestrator, _process_source
from app.research.telemetry import COUNTERS, TIMINGS, count, person_performance, stage
from app.retrieval.service import RetrievalService, RetrievedPage, StaticFetcher
from app.schemas import (
    ExtractionResponse,
    PersonSeed,
    ProfileField,
    ResearchMetrics,
    SourceCandidate,
    SourceRecord,
    UsageRecord,
)
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
@pytest.mark.parametrize("valid_at_limit", [False, True])
async def test_deepseek_output_budget_and_recovery_use_only_one_luna_fallback(valid_at_limit):
    settings = configured(OPENROUTER_MAX_RETRIES=1).model_copy(
        update={"OPENROUTER_EXTRACTION_MODEL": DEEPSEEK_EXTRACTION_MODEL}
    )
    metrics, usage, requests = ResearchMetrics(), [], []
    content = '{"claims":[{"field":"full_name","value":"Jane Doe","evidence":"Jane Doe","subject_name":"Jane Doe"}]}'
    snapshot = None

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body["model"] == DEEPSEEK_EXTRACTION_MODEL:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": content if valid_at_limit else '{"claims":['},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 6000, "cost": 0.01},
                },
            )
        return response(content)

    async def checkpoint():
        nonlocal snapshot
        if len(usage) == 1 and any(
            receipt["state"] == "output_limited" for receipt in metrics.recovery["requests"].values()
        ):
            snapshot = snapshot or metrics.model_copy(deep=True)

    async def extract(client):
        return [
            (claims.claims[0].value, model)
            async for _, claims, model in extract_chunks(
                client,
                PersonSeed(full_name="Jane Doe"),
                ["Jane Doe"],
                "https://example.org/jane",
                CONTEXT,
                asyncio.Semaphore(1),
                1,
            )
        ]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenRouterClient(settings, http)
        client = BudgetedModel(provider, settings, metrics, usage)
        client.checkpoint = checkpoint
        expected_model = DEEPSEEK_EXTRACTION_MODEL if valid_at_limit else LUNA_EXTRACTION_FALLBACK
        assert await extract(client) == [("Jane Doe", expected_model)]
        assert requests[0]["max_tokens"] == 6000
        assert requests[0]["reasoning"] == {"enabled": False}
        assert requests[0]["response_format"]["type"] == "json_schema"
        assert "shortest contiguous literal evidence" in requests[0]["messages"][0]["content"]
        assert len(requests) == (1 if valid_at_limit else 2)
        if not valid_at_limit:
            assert snapshot is not None
            assert requests[1]["model"] == LUNA_EXTRACTION_FALLBACK
            assert requests[1]["max_tokens"] == settings.OPENROUTER_MAX_TOKENS
            assert usage[0].exception_type == "OutputTokenLimit"
            assert usage[1].fallback_from_model == DEEPSEEK_EXTRACTION_MODEL
            assert (
                json.loads(requests[0]["messages"][1]["content"])["payload"]
                == json.loads(requests[1]["messages"][1]["content"])["payload"]
            )
            after_primary_usage = usage[:1]
            assert await extract(BudgetedModel(provider, settings, snapshot, after_primary_usage)) == [
                ("Jane Doe", expected_model)
            ]
            assert [body["model"] for body in requests] == [
                DEEPSEEK_EXTRACTION_MODEL,
                LUNA_EXTRACTION_FALLBACK,
                LUNA_EXTRACTION_FALLBACK,
            ]
            assert len(after_primary_usage) == snapshot.llm_calls == 2
            assert snapshot.cost == 0.01
        completed = metrics.model_copy(deep=True)
        persisted_usage = usage.copy()
        requests_before = len(requests)
        assert await extract(BudgetedModel(provider, settings, completed, persisted_usage)) == [
            ("Jane Doe", expected_model)
        ]
        assert len(requests) == requests_before
        assert persisted_usage == usage


@pytest.mark.asyncio
async def test_extraction_preserves_sibling_replies_when_primary_and_fallback_exhaust_output():
    settings = configured(OPENROUTER_MAX_RETRIES=0).model_copy(
        update={"OPENROUTER_EXTRACTION_MODEL": DEEPSEEK_EXTRACTION_MODEL}
    )
    models = []

    async def handler(request):
        body = json.loads(request.content)
        models.append(body["model"])
        content = json.loads(body["messages"][1]["content"])["payload"]["content"]
        if content == "first":
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"claims":['}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": body["max_tokens"]},
                },
            )
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BudgetedModel(OpenRouterClient(settings, http), settings, ResearchMetrics(), [])
        accepted = []
        with pytest.raises(OpenRouterError) as caught:
            async for chunk, _, _ in extract_chunks(
                client,
                PersonSeed(full_name="Jane Doe"),
                ["first", "second"],
                "https://example.org/jane",
                CONTEXT,
                asyncio.Semaphore(1),
                1,
            ):
                accepted.append(chunk)
    assert caught.value.exception_type == "OutputTokenLimit"
    assert accepted == ["second"]
    assert models == [DEEPSEEK_EXTRACTION_MODEL, LUNA_EXTRACTION_FALLBACK, DEEPSEEK_EXTRACTION_MODEL]


@pytest.mark.asyncio
async def test_permanent_provider_denial_drains_successful_sibling_without_new_http_calls():
    settings = configured(OPENROUTER_MAX_RETRIES=0)
    requested = []
    sibling_started = asyncio.Event()

    async def handler(request):
        body = json.loads(request.content)
        content = json.loads(body["messages"][1]["content"])["payload"]["content"]
        requested.append(content)
        if content == "denied":
            await sibling_started.wait()
            return httpx.Response(403, json={"error": {"code": 403, "message": "Forbidden"}})
        sibling_started.set()
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BudgetedModel(OpenRouterClient(settings, http), settings, ResearchMetrics(), [])
        accepted = []
        async with asyncio.timeout(2):
            with pytest.raises(OpenRouterError) as caught:
                async for chunk, _, _ in extract_chunks(
                    client,
                    PersonSeed(full_name="Jane Doe"),
                    ["denied", "successful sibling", "blocked before dispatch"],
                    "https://example.org/jane",
                    CONTEXT,
                    asyncio.Semaphore(2),
                    2,
                ):
                    accepted.append(chunk)
    assert caught.value.http_status == 403
    assert accepted == ["successful sibling"]
    assert requested == ["denied", "successful sibling"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["complete", "search"])
async def test_durable_paid_reply_is_reused_after_metrics_reload(operation):
    settings = configured(OPENROUTER_MAX_RETRIES=0)
    metrics, usage, snapshots = ResearchMetrics(), [], []
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if operation == "search":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": "https://example.org/jane",
                                            "title": "Jane Doe",
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                },
            )
        return response()

    async def invoke(budget, provider):
        if operation == "search":
            return await budget.search(OpenRouterSearchProvider(settings, provider), "Jane Doe", 1, CONTEXT)
        return await budget.complete(
            ExtractionResponse,
            "extraction",
            EXTRACTION_SYSTEM_PROMPT,
            {},
            CONTEXT,
            EXTRACTION_PROMPT_VERSION,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenRouterClient(settings, http)
        budget = BudgetedModel(provider, settings, metrics, usage)

        async def checkpoint():
            snapshots.append((metrics.model_dump(mode="json"), [x.model_dump(mode="json") for x in usage]))

        budget.checkpoint = checkpoint
        original = await invoke(budget, provider)
        saved_metrics, saved_usage = snapshots[-1]
        resumed_metrics = ResearchMetrics.model_validate(saved_metrics)
        resumed_usage = [UsageRecord.model_validate(x) for x in saved_usage]
        resumed = BudgetedModel(provider, settings, resumed_metrics, resumed_usage)
        resumed.checkpoint = checkpoint
        cached = await invoke(resumed, provider)
    assert calls == 1
    assert cached == original
    assert resumed_metrics.llm_calls == 1
    assert len(resumed_usage) == 1
    if operation == "complete":
        assert isinstance(cached, tuple) and isinstance(cached[0], ExtractionResponse)
        assert isinstance(cached[1][0], UsageRecord)
    else:
        assert isinstance(cached, list) and isinstance(cached[0], SourceCandidate)
    assert all(receipt["state"] == "completed" for receipt in resumed_metrics.recovery["requests"].values())


@pytest.mark.asyncio
async def test_uncertain_paid_request_stays_blocked_after_metrics_reload():
    settings = configured(OPENROUTER_MAX_RETRIES=2)
    metrics, usage, snapshots = ResearchMetrics(), [], []
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("acknowledgement lost", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenRouterClient(settings, http)
        budget = BudgetedModel(provider, settings, metrics, usage)

        async def checkpoint():
            snapshots.append(metrics.model_dump(mode="json"))

        budget.checkpoint = checkpoint
        arguments = (
            ExtractionResponse,
            "extraction",
            EXTRACTION_SYSTEM_PROMPT,
            {},
            CONTEXT,
            EXTRACTION_PROMPT_VERSION,
        )
        with pytest.raises(OpenRouterError) as caught:
            await budget.complete(*arguments)
        assert caught.value.response_uncertain is True
        resumed_metrics = ResearchMetrics.model_validate(snapshots[-1])
        resumed = BudgetedModel(provider, settings, resumed_metrics, usage)
        resumed.checkpoint = checkpoint
        with pytest.raises(BudgetExceeded, match="PROVIDER_RESPONSE_UNCERTAIN"):
            await resumed.complete(*arguments)
    assert calls == len(usage) == resumed_metrics.llm_calls == 1
    assert resumed_metrics.tokens_budgeted > 0


@pytest.mark.asyncio
async def test_only_proven_unsent_connection_attempts_release_paid_reservations():
    settings = configured(OPENROUTER_MAX_RETRIES=2)
    metrics, usage = ResearchMetrics(), []

    async def handler(request):
        raise httpx.ConnectTimeout("temporarily unavailable", request=request)

    async def no_sleep(_):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        budget = BudgetedModel(OpenRouterClient(settings, http, sleep=no_sleep), settings, metrics, usage)
        budget.checkpoint = lambda: asyncio.sleep(0)
        with pytest.raises(OpenRouterError):
            await budget.complete(
                ExtractionResponse,
                "extraction",
                EXTRACTION_SYSTEM_PROMPT,
                {},
                CONTEXT,
                EXTRACTION_PROMPT_VERSION,
            )
    assert len(usage) == 3
    assert metrics.llm_calls == metrics.tokens_budgeted == metrics.tokens_used == 0
    assert metrics.recovery["requests"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unsent", "rate_limited"])
async def test_crash_after_definitive_failure_checkpoint_keeps_request_recoverable(failure):
    settings = configured(OPENROUTER_MAX_RETRIES=0)
    metrics, usage, snapshots = ResearchMetrics(), [], []
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "unsent":
                raise httpx.ConnectTimeout("temporarily unavailable", request=request)
            return httpx.Response(429, json={"error": {"code": 429}})
        assert all(x["state"] == "in_flight" for x in resumed_metrics.recovery["requests"].values())
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenRouterClient(settings, http)
        budget = BudgetedModel(provider, settings, metrics, usage)

        async def checkpoint_then_crash():
            snapshots.append((metrics.model_dump(mode="json"), [x.model_dump(mode="json") for x in usage]))
            if usage:
                raise asyncio.CancelledError()

        budget.checkpoint = checkpoint_then_crash
        arguments = (
            ExtractionResponse,
            "extraction",
            EXTRACTION_SYSTEM_PROMPT,
            {},
            CONTEXT,
            EXTRACTION_PROMPT_VERSION,
        )
        with pytest.raises(asyncio.CancelledError):
            await budget.complete(*arguments)
        saved_metrics, saved_usage = snapshots[-1]
        resumed_metrics = ResearchMetrics.model_validate(saved_metrics)
        resumed_usage = [UsageRecord.model_validate(x) for x in saved_usage]
        assert all(x["state"] == "retryable" for x in resumed_metrics.recovery["requests"].values())
        resumed = BudgetedModel(provider, settings, resumed_metrics, resumed_usage)
        resumed.checkpoint = lambda: asyncio.sleep(0)
        parsed, _ = await resumed.complete(*arguments)
    assert isinstance(parsed, ExtractionResponse)
    assert calls == len(resumed_usage) == 2
    assert resumed_metrics.llm_calls == (1 if failure == "unsent" else 2)
    assert all(x["state"] == "completed" for x in resumed_metrics.recovery["requests"].values())


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
async def test_applied_extraction_is_skipped_before_call_without_reindexing_pending_chunks():
    settings = configured(OPENROUTER_MAX_RETRIES=0)
    metrics, usage, indexes = ResearchMetrics(), [], []
    context = dict(CONTEXT, source_id="source-1")
    url = "https://example.org/jane"
    applied = {
        extraction_chunk_key(
            context["source_id"],
            "first",
            person_id=context["person_id"],
            source_url=url,
            model=settings.OPENROUTER_EXTRACTION_MODEL,
        )
    }

    async def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])["payload"]
        indexes.append(payload["chunk_index"])
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BudgetedModel(OpenRouterClient(settings, http), settings, metrics, usage)
        async with aclosing(
            extract_chunks(
                client,
                PersonSeed(full_name="Jane Doe"),
                ["first", "second"],
                url,
                context,
                asyncio.Semaphore(1),
                1,
                applied_chunks=applied,
            )
        ) as chunks:
            output = [chunk async for chunk, _, _ in chunks]
    assert indexes == [1] and output == ["second"]
    assert metrics.llm_calls == len(usage) == 1
    assert metrics.extraction_cache_hits == 1


def test_extraction_applied_key_is_identity_content_url_model_and_rule_scoped(monkeypatch):
    import app.research.extraction as extraction

    values = {
        "source_id": "source-1",
        "chunk": "Jane Doe graduated from Example University.",
        "person_id": "person-1",
        "source_url": "https://example.org/jane?utm_source=search#education",
        "model": "configured-model",
    }
    key = extraction_chunk_key(**values)
    assert extraction_chunk_key(**(values | {"source_url": "https://example.org/jane"})) == key
    for name, replacement in (
        ("source_id", "source-2"),
        ("person_id", "person-2"),
        ("source_url", "https://example.org/another-page"),
        ("chunk", values["chunk"] + " New evidence."),
        ("model", "another-model"),
    ):
        assert extraction_chunk_key(**(values | {name: replacement})) != key
    monkeypatch.setattr(extraction, "EXTRACTION_PROMPT_VERSION", "changed-rules")
    assert extraction_chunk_key(**values) != key


@pytest.mark.asyncio
async def test_extraction_out_of_order_is_bounded_and_consumed_in_chunk_order():
    settings = configured(OPENROUTER_MAX_RETRIES=0)
    metrics, usage = ResearchMetrics(), []
    active = peak = 0
    finished = []
    people = []
    second = asyncio.Event()

    async def handler(request):
        nonlocal active, peak
        envelope = json.loads(json.loads(request.content)["messages"][1]["content"])["payload"]
        index = envelope["chunk_index"]
        people.append(envelope["person"])
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
                    PersonSeed(
                        full_name="Jane Doe",
                        known_attributes={
                            "source_dataset": "September import",
                            "alumni_organisation": "Example Alumni Network",
                        },
                    ),
                    ["first", "second", "third"],
                    "https://example.org/jane",
                    CONTEXT,
                    asyncio.Semaphore(2),
                    2,
                )
            ) as chunks:
                output = [chunk async for chunk, _, _ in chunks]
    assert output == ["first", "second", "third"]
    assert finished == [1, 0, 2]
    assert all(
        person["known_attributes"] == {"alumni_organisation": "Example Alumni Network"} for person in people
    )
    assert peak == 2 and active == 0
    assert len(usage) == metrics.llm_calls == 3
    assert metrics.tokens_used == metrics.tokens_budgeted == 90
    assert len(checkpoints) == 9
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
    # Exact in-flight duplicates are fenced. The owning request can exhaust its
    # bounded retries with a provider error without exceeding either budget.
    assert all(isinstance(outcome, (BudgetExceeded, OpenRouterError)) for outcome in outcomes)
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
                async for chunk, _, _ in chunks:
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
@pytest.mark.parametrize("error", [RuntimeError, TimeoutError])
async def test_lease_renewal_failure_cancels_owned_research_before_terminal_write(error):
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
    assert failures == ["PERSISTENCE_FAILED"]


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
            return httpx.Response(
                200,
                json={
                    "requested_url": url,
                    "final_url": urls[1] if redirect_source and url == urls[0] else url,
                    "status": 502 if failed_source and url == urls[0] else 200,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("education_found", [True, False])
async def test_missing_education_is_searched_before_broad_candidates_exhaust_source_cap(
    monkeypatch, education_found
):
    async def safe(url, settings=None):
        return url

    monkeypatch.setattr("app.retrieval.service.validate_public_url", safe)
    settings = configured(
        MAX_SOURCES_PER_PERSON=4,
        SOURCES_PER_ROUND=2,
        MAX_CONCURRENT_FETCHES=1,
        MAX_SEARCH_QUERIES_PER_PERSON=2,
        MAX_SOURCE_MODEL_TOOL_CALLS=2,
        MAX_SEARCH_RESULTS_PER_QUERY=6,
        OPENROUTER_MAX_RETRIES=0,
        PLAYWRIGHT_ENABLED=False,
        STATIC_FETCH_WORKER_URL="https://worker.example.org/fetch",
        STATIC_FETCH_WORKER_SECRET="fixture-secret",
    )
    broad_urls = [f"https://work{index}.example.org/jane" for index in range(6)]
    education_url = "https://education.example.org/jane"
    work_quote = "Jane Doe is Research Director at Example Foundation."
    education_quote = "Jane Doe works at Example Foundation and earned BSc in Physics at Example University."
    events, validation_batches = [], []

    async def fetch(request):
        url = json.loads(request.content)["url"]
        events.append(("fetch", url))
        quote = education_quote if url == education_url else work_quote
        return httpx.Response(
            200,
            json={
                "requested_url": url,
                "final_url": url,
                "status": 200,
                "content_type": "text/html",
                "html": f"<h1>Jane Doe</h1><p>{quote}</p><p>{url}</p>",
            },
        )

    async def model(request):
        body = json.loads(request.content)
        envelope = json.loads(body["messages"][1]["content"])
        if body.get("tools"):
            query = envelope["query"]
            events.append(("search", query))
            targeted = "education" in query
            urls = ([education_url] if education_found else []) if targeted else broad_urls
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
                                        "url_citation": {
                                            "url": url,
                                            "title": "Jane Doe | Example Foundation",
                                        },
                                    }
                                    for url in urls
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                },
            )
        payload = envelope["payload"]
        if payload.get("task") == "validate_candidates":
            validation_batches.append([candidate["url"] for candidate in payload["candidates"]])
            answer = {
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "source_type": "university" if candidate["url"] == education_url else "employer",
                        "relevance": "likely",
                    }
                    for candidate in payload["candidates"]
                ]
            }
        else:
            assert "source_url" in payload, (
                "An optional planning call should not replace deterministic targeting"
            )
            educational = payload["source_url"] == education_url
            quote = education_quote if educational else work_quote
            values = (
                [("university_name", "Example University"), ("degree_type", "BSc"), ("subject", "Physics")]
                if educational
                else [("organisation", "Example Foundation"), ("job_title", "Research Director")]
            )
            answer = {
                "claims": [
                    {
                        "field": field,
                        "value": value,
                        "evidence": quote,
                        "subject_name": "Jane Doe",
                        "fact_group": "education" if educational else "employment",
                        "is_current": not educational,
                    }
                    for field, value in [("full_name", "Jane Doe"), *values]
                ]
            }
        return response(json.dumps(answer))

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as fetch_http:
            provider = OpenRouterClient(settings, model_http)
            retrieval = RetrievalService(settings, StaticFetcher(settings, fetch_http))
            pipeline = ResearchOrchestrator(
                settings, OpenRouterSearchProvider(settings, provider), provider, retrieval
            )
            result = await pipeline.research(
                "job", "person", PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
            )
            await retrieval.close()

    searches = [(index, value) for index, (kind, value) in enumerate(events) if kind == "search"]
    fetched = [value for kind, value in events if kind == "fetch"]
    assert len(searches) == 2 and "education" in searches[1][1]
    assert [kind for kind, _ in events[: searches[1][0]]] == ["search", "fetch", "fetch"]
    assert fetched[:2] == broad_urls[:2]
    assert len(fetched) == result.profile.metrics.sources_fetched == settings.MAX_SOURCES_PER_PERSON
    assert result.profile.metrics.field_specific_searches == 1
    assert result.profile.metrics.accounting["field_specific_searches"] == 1
    assert result.profile.metrics.accounting["field_specific_searches_with_support"] == int(education_found)
    assert result.profile.metrics.accounting["actual_api_cost"] is None  # Provider fixture omits cost.
    assert validation_batches[0] == broad_urls  # Pending broad candidates reuse their first validation.
    assert len(validation_batches) == 2 if education_found else len(validation_batches) == 1
    if education_found:
        assert fetched[2] == education_url
        assert result.profile.fields[ProfileField.university_name].researched_value == "Example University"
        assert result.profile.fields[ProfileField.degree_type].researched_value == "Bachelor's Degree"
    else:
        assert fetched == broad_urls[:4]  # An empty targeted search does not discard useful queued evidence.
    assert result.profile.metrics.stop_reason == "MAX_SOURCES"


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous_paragraph", [False, True])
async def test_grounded_structured_facts_avoid_extraction_but_ambiguous_prose_still_uses_model(
    monkeypatch, ambiguous_paragraph
):
    async def safe(url, settings=None):
        return url

    monkeypatch.setattr("app.retrieval.service.validate_public_url", safe)
    settings = configured(
        MAX_SOURCES_PER_PERSON=1,
        OPENROUTER_MAX_RETRIES=0,
        PLAYWRIGHT_ENABLED=False,
        STATIC_FETCH_WORKER_URL="https://worker.example.org/fetch",
        STATIC_FETCH_WORKER_SECRET="fixture-secret",
    )
    url = "https://employer.example.org/jane"
    structured = {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": "Jane Doe",
        "worksFor": {"@type": "Organization", "name": "Example Foundation"},
        "jobTitle": "Research Director",
    }
    quote = "Jane Doe studied at Example University; the qualification is not stated."
    html = f'<script type="application/ld+json">{json.dumps(structured)}</script>'
    if ambiguous_paragraph:
        html += f"<p>{quote}</p>"
    extraction_inputs = []

    async def fetch(request):
        return httpx.Response(
            200,
            json={
                "requested_url": url,
                "final_url": url,
                "status": 200,
                "content_type": "text/html",
                "html": html,
            },
        )

    async def model(request):
        body = json.loads(request.content)
        assert not body.get("tools"), "The unchanged one-source budget prevents new discovery"
        payload = json.loads(body["messages"][1]["content"])["payload"]
        if payload.get("task") == "validate_candidates":
            answer = {
                "decisions": [
                    {"candidate_id": item["candidate_id"], "source_type": "employer", "relevance": "likely"}
                    for item in payload["candidates"]
                ]
            }
        else:
            extraction_inputs.append(payload["content"])
            answer = {
                "claims": [
                    {
                        "field": "university_name",
                        "value": "Example University",
                        "evidence": quote,
                        "subject_name": "Jane Doe",
                    }
                ]
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "cost": 0.002},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as fetch_http:
            provider = OpenRouterClient(settings, model_http)
            retrieval = RetrievalService(settings, StaticFetcher(settings, fetch_http))
            pipeline = ResearchOrchestrator(
                settings, OpenRouterSearchProvider(settings, provider), provider, retrieval
            )
            result = await pipeline.research(
                "job",
                "person",
                PersonSeed(full_name="Jane Doe", organisation="Example Foundation", preferred_urls=[url]),
            )
            await retrieval.close()

    assert result.sources[0].processing_status == "extracted"
    assert result.sources[0].identity.score >= settings.SCORING.identity_review_threshold
    assert result.profile.fields[ProfileField.organisation].researched_value == "Example Foundation"
    assert result.profile.fields[ProfileField.job_title].researched_value == "Research Director"
    assert result.profile.metrics.deterministic_claims_grounded == 3
    accounting = result.profile.metrics.accounting
    assert accounting["deterministic_claims_extracted"] == 3
    assert accounting["deterministic_claims_grounded"] == 3
    assert accounting["deterministic_claims_accepted"] == 3
    assert accounting["llm_extraction_escalations"] == int(ambiguous_paragraph)
    assert accounting["actual_api_cost"] == sum(usage.cost for usage in result.usage)
    assert accounting["actual_api_cost"] == 0.002 * (1 + int(ambiguous_paragraph))
    assert len(extraction_inputs) == int(ambiguous_paragraph)
    if ambiguous_paragraph:
        assert quote in extraction_inputs[0]
        assert "worksFor" not in extraction_inputs[0]
        assert result.profile.fields[ProfileField.university_name].researched_value == "Example University"
        assert result.profile.fields[ProfileField.degree_type].researched_value is None
    else:
        assert result.profile.metrics.extraction_calls_avoided == 1


@pytest.mark.asyncio
async def test_exact_in_memory_receipts_reuse_usage_and_provider_outage_stops_new_calls():
    settings = configured(OPENROUTER_MAX_RETRIES=0)
    metrics, usage = ResearchMetrics(), []
    requests = 0

    async def handler(request):
        nonlocal requests
        requests += 1
        if requests == 2:
            return httpx.Response(403, json={"error": {"message": "provider unavailable"}})
        return response()

    args = (ExtractionResponse, "extraction", "prompt", {"content": "first"}, CONTEXT, "v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BudgetedModel(OpenRouterClient(settings, http), settings, metrics, usage)
        await client.complete(*args)
        with pytest.raises(OpenRouterError):
            await client.complete(*args[:3], {"content": "blocked"}, *args[4:])
        assert client.provider_unavailable and client.provider_unavailable_status == 403
        await client.complete(*args)
        with pytest.raises(BudgetExceeded, match="PROVIDER_UNAVAILABLE"):
            await client.complete(*args[:3], {"content": "another"}, *args[4:])
    assert requests == len(usage) == metrics.llm_calls == 2
    assert metrics.request_cache_hits == 1


def test_cumulative_accounting_distinguishes_unknown_cost_input_certainty_and_credentials():
    from app.research.telemetry import aggregate_person_summaries, bind_research_metrics, record_person_result
    from app.schemas import (
        EvidenceClaim,
        FieldDecision,
        PersonProfile,
        ProfileField,
        ProfileRecord,
        ResearchResult,
    )

    supplied = FieldDecision(
        value="Jane Doe", confidence=100, research_confidence=0, provenance_status="supplied"
    )
    organisation = FieldDecision(
        value="Verified Institute",
        confidence=81,
        sources=["https://example.org/person"],
        provenance_status="researched",
        review_required=False,
    )
    university = FieldDecision(
        value="Verified University",
        confidence=91,
        sources=["https://example.org/degree"],
        provenance_status="researched",
        review_required=False,
    )
    fields = {ProfileField.full_name: supplied, ProfileField.organisation: organisation}
    second = ProfileRecord(
        record_id="education",
        fields={**fields, ProfileField.university_name: university},
        profile_confidence=91,
        coverage=33.33,
        review_required=False,
    )
    profile = PersonProfile(
        person_id="person",
        input_name="Jane Doe",
        fields=fields,
        records=[second],
        profile_confidence=81,
        coverage=16.67,
        review_required=False,
    )
    paid = UsageRecord(
        job_id="job",
        person_id="person",
        role="source",
        operation="web_search",
        model="source-model",
        prompt_version="v1",
        prompt_tokens=10,
        completion_tokens=20,
        cost=0.12,
        web_search_requests=1,
    )
    unknown = UsageRecord(
        job_id="job",
        person_id="person",
        role="extraction",
        operation="extract_claims",
        model="extract-model",
        prompt_version="v1",
        prompt_tokens=30,
        completion_tokens=40,
    )
    result = ResearchResult(profile=profile, usage=[paid, paid.model_copy(), unknown])
    result.profile.metrics.llm_calls = 2
    result.profile.metrics.retrieval_cache_hits = 1
    with person_performance("job", "person"):
        bind_research_metrics(result.profile.metrics)
        count("retrieval_cache_hits")
        count("retrieval_cache_misses", 2)
        summary = record_person_result(result)
    assert summary["model_calls"] == 2
    assert summary["input_tokens"] == 40 and summary["output_tokens"] == 60
    assert summary["reported_api_cost"] == 0.12
    assert summary["actual_api_cost"] is None and not summary["cost_complete"]
    assert summary["operations"]["web_search"]["actual_api_cost"] == 0.12
    assert summary["operations"]["extract_claims"]["actual_api_cost"] is None
    assert summary["independently_supported_fields"] == ["organisation", "university_name"]
    assert "full_name" in summary["unresolved_required_fields"]
    assert summary["sources_used_as_evidence"] == 2
    assert summary["retrieval_cache_hits"] == summary["retrieval_cache_misses"] == 2
    assert summary["source_reuse_rate"] == 0.5
    assert result.profile.metrics.accounting == summary
    aggregate = aggregate_person_summaries([summary])
    assert aggregate["average_actual_cost_per_attempted_person"] is None
    assert aggregate["average_model_calls_per_person"] == 2
    assert aggregate["independent_field_coverage_rate"] == pytest.approx(2 / 7, abs=1e-6)
    result.usage[-1].cost = 0.08
    measured = record_person_result(result)
    aggregate = aggregate_person_summaries([measured, measured])
    assert aggregate["actual_api_cost"] == 0.4
    assert aggregate["average_actual_cost_per_attempted_person"] == 0.2
    assert aggregate["average_actual_cost_per_enriched_person"] == 0.2
    assert aggregate["average_actual_cost_per_supported_field"] == 0.1
    incomplete = aggregate_person_summaries([measured], unmeasured_people=1)
    assert incomplete["attempted_people"] == 2 and incomplete["unmeasured_people"] == 1
    assert incomplete["actual_api_cost"] is None
    assert incomplete["average_model_calls_per_person"] is None
    name = result.profile.records[0].fields[ProfileField.full_name]
    name.research_confidence = 74
    name.provenance_status = "corroborated"
    name.scoring_components["seed_corroborating_claim_ids"] = ["name-claim"]
    result.claims.append(
        EvidenceClaim(
            claim_id="name-claim",
            person_id="person",
            source_id="name-source",
            field=ProfileField.full_name,
            raw_value="Jane Doe",
            normalised_value="jane doe",
            evidence_text="Jane Doe works at Verified Institute.",
            subject_name="Jane Doe",
            extraction_model="extract-model",
        )
    )
    assert "full_name" in record_person_result(result)["independently_supported_fields"]
    result.claims[0].normalised_value = "other person"
    assert "full_name" not in record_person_result(result)["independently_supported_fields"]
