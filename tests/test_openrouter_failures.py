from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.logging import JsonFormatter
from app.providers.openrouter import OpenRouterClient, OpenRouterError, strict_json_schema
from app.providers.search import OpenRouterSearchProvider, SearchProviderError
from app.providers.source_advisor import SourceAdvisor, SourceDecisionSet
from app.research.budget import BudgetedModel
from app.schemas import Contract, PersonSeed, ResearchMetrics, UsageRecord


class DiagnosticResponse(Contract):
    name: str


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason,completion_tokens", [("length", 100), (None, 3000)])
async def test_truncated_structured_output_is_not_retried(finish_reason, completion_tokens):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": finish_reason, "message": {"content": '{"name":'}}],
                "usage": {"prompt_tokens": 2163, "completion_tokens": completion_tokens},
            },
        )

    error, emitted = await complete(handler, retries=2)
    assert len(attempts) == len(emitted) == 1
    assert error.retryable is False
    assert error.error_code == "OPENROUTER_SCHEMA_ERROR"
    assert error.exception_type == "OutputTokenLimit"
    assert emitted[0].completion_tokens == completion_tokens
    assert "token limit" in str(error)


@pytest.mark.asyncio
async def test_typed_provider_output_limit_is_not_retried():
    def handler(request):
        return httpx.Response(
            200, json={"error": {"code": 400, "metadata": {"error_type": "max_tokens_exceeded"}}}
        )

    error, emitted = await complete(handler, retries=2)
    assert len(emitted) == 1 and error.exception_type == "OutputTokenLimit"
    assert emitted[0].http_status == 200 and emitted[0].provider_error_status == 400
    assert emitted[0].provider_error_type == "max_tokens_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("usage,choices", [("invalid", 42), ([], None), (None, {})])
async def test_malformed_envelope_does_not_mask_schema_error(usage, choices):
    def handler(request):
        return httpx.Response(200, json={"choices": choices, "usage": usage})

    error, emitted = await complete(handler)
    assert error.error_code == "OPENROUTER_SCHEMA_ERROR"
    assert len(emitted) == 1


async def no_sleep(_: float) -> None:
    return None


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "APP_ENV": "test",
        "OPENROUTER_API_KEY": SecretStr("openrouter-token"),
        "OPENROUTER_SOURCE_MODEL": "source-model",
        "OPENROUTER_EXTRACTION_MODEL": "extraction-model",
        "OPENROUTER_MAX_RETRIES": 0,
    }
    values.update(overrides)
    return Settings(**values)


async def complete(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    retries: int = 0,
) -> tuple[OpenRouterError, list[UsageRecord]]:
    emitted: list[UsageRecord] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenRouterClient(
            settings(OPENROUTER_MAX_RETRIES=retries),
            client=http_client,
            sleep=no_sleep,
        )
        with pytest.raises(OpenRouterError) as caught:
            await client.complete(
                DiagnosticResponse,
                "source",
                "Return JSON.",
                {"task": "validate_candidates"},
                {"job_id": "job-1", "person_id": "person-1"},
                "source-advisor-v1",
                on_usage=emitted.append,
            )
    return caught.value, emitted


def assert_zero_token_diagnostics(
    error: OpenRouterError,
    usage: UsageRecord,
    *,
    error_code: str,
    http_status: int | None,
    exception_type: str,
    response_received: bool,
    response_body_received: bool,
    request_sent: bool = True,
) -> None:
    assert error.error_code == error_code
    assert error.http_status == http_status
    assert usage.operation == "validate_candidates"
    assert usage.error_code == error_code
    assert usage.http_status == http_status
    assert usage.exception_type == exception_type
    assert usage.retry_attempt == 0
    assert usage.request_sent is request_sent
    assert usage.response_received is response_received
    assert usage.response_body_received is response_body_received
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.cost is None
    assert usage.success is False


def test_strict_json_schema_recursively_removes_defaults() -> None:
    schema = strict_json_schema(SourceDecisionSet)
    dictionaries: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            dictionaries.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)

    assert dictionaries
    assert all("default" not in node for node in dictionaries)
    object_nodes = [node for node in dictionaries if "properties" in node]
    assert object_nodes
    assert all(node.get("additionalProperties") is False for node in object_nodes)
    assert all(set(node["required"]) == set(node["properties"]) for node in object_nodes)


@pytest.mark.asyncio
async def test_http_400_records_safe_zero_token_diagnostics() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 400, "message": "private invalid-schema detail"}},
            request=request,
        )

    error, emitted = await complete(handler)

    assert len(error.usage) == len(emitted) == 1
    assert error.usage == emitted
    assert_zero_token_diagnostics(
        error,
        emitted[0],
        error_code="OPENROUTER_HTTP_ERROR",
        http_status=400,
        exception_type="OpenRouterError",
        response_received=True,
        response_body_received=True,
    )
    assert error.retryable is False
    assert "private invalid-schema detail" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,metadata,category",
    [
        ("API key is disabled", {}, "authentication"),
        ("Missing permission for this API key", {}, "key_restricted"),
        ("Budget limit exceeded", {"limit_source": "openrouter_key_limit"}, "spending_limit"),
        ("Model is not allowed", {}, "route_restricted"),
        ("Forbidden", {"error_type": "permission_denied", "provider_name": "Inference A"}, "provider_access"),
        (
            "Forbidden",
            {"error_type": "content_policy_violation", "flagged_input": "private input"},
            "content_policy",
        ),
        ("Forbidden", {}, "forbidden_unclassified"),
    ],
)
async def test_forbidden_error_preserves_only_allowlisted_details(message, metadata, category):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(
            403,
            headers={"X-OpenRouter-Request-Id": "req-123-test", "Authorization": "Bearer raw-secret"},
            json={
                "error": {
                    "code": 403,
                    "message": f"{message}. Echoed Bearer openrouter-token.",
                    "metadata": {**metadata, "raw": "raw-secret"},
                },
            },
        )

    error, usage = await complete(handler, retries=2)
    assert len(attempts) == len(usage) == 1
    assert error.provider_error_category == usage[0].provider_error_category == category
    assert error.retryable is False and error.transient is False
    assert usage[0].provider_error_status == 403
    assert usage[0].provider_request_id == "req-123-test"
    assert usage[0].provider_limit_source == metadata.get("limit_source")
    log = logging.makeLogRecord({"msg": "model_attempt", **usage[0].model_dump()})
    safe_details = JsonFormatter().format(log) + usage[0].model_dump_json() + str(error)
    assert "raw-secret" not in safe_details and "openrouter-token" not in safe_details
    assert "private input" not in safe_details and "Echoed" not in safe_details


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "access_message,next_requests", [("API key is disabled", 0), ("Model not allowed", 1)]
)
async def test_access_cooldown_stops_rejected_scope_without_counting_a_request(access_message, next_requests):
    models, usage = [], []

    def handler(request):
        body = json.loads(request.content)
        models.append(body["model"])
        if len(models) == 1:
            return httpx.Response(403, json={"error": {"code": 403, "message": access_message}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"name":"Jane"}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenRouterClient(settings(), http_client)
        args = (DiagnosticResponse, "extraction", "Return JSON.", {}, {}, "test")
        with pytest.raises(OpenRouterError):
            await client.complete(*args, on_usage=usage.append)
        with pytest.raises(OpenRouterError, match="temporarily blocked"):
            await client.complete(*args, on_usage=usage.append)
        if next_requests:
            response, _ = await client.complete(
                *args, model_override="alternate/model", on_usage=usage.append
            )
            assert response.name == "Jane"
        else:
            with pytest.raises(OpenRouterError, match="temporarily blocked"):
                await client.complete(*args, model_override="alternate/model", on_usage=usage.append)
    assert len(models) == len(usage) == 1 + next_requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,metadata,category",
    [
        ("Model not allowed", {}, "route_restricted"),
        ("Forbidden", {"error_type": "content_policy_violation"}, "content_policy"),
    ],
)
async def test_recovered_search_keeps_access_receipts_scoped(message, metadata, category):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                403, json={"error": {"code": 403, "message": message, "metadata": metadata}}
            )
        return httpx.Response(200, json={"choices": [{"message": {"annotations": []}}]})

    config = settings()
    ledger = []
    context = {"job_id": "job-1", "person_id": "person-1"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        original_client = OpenRouterClient(config, http)
        original = BudgetedModel(original_client, config, ResearchMetrics(), ledger)
        with pytest.raises(SearchProviderError):
            await original.search(OpenRouterSearchProvider(config, original_client), "same", 1, context)

        recovered_client = OpenRouterClient(config, http)
        recovered = BudgetedModel(
            recovered_client, config, original.metrics.model_copy(deep=True), list(ledger)
        )
        provider = OpenRouterSearchProvider(config, recovered_client)
        with pytest.raises(SearchProviderError) as caught:
            await recovered.search(provider, "same", 1, context)
        assert caught.value.provider_error_category == category
        assert len(requests) == len(recovered.usage) == 1
        if category == "route_restricted":
            with pytest.raises(SearchProviderError) as blocked:
                await recovered.search(provider, "different", 1, context)
            assert blocked.value.provider_error_category == category
            assert len(requests) == len(recovered.usage) == 1
        else:
            assert await recovered.search(provider, "different", 1, context) == []
            assert len(requests) == len(recovered.usage) == 2


@pytest.mark.asyncio
async def test_route_pinning_and_reasoning_metadata_keep_the_alternate_route_independent():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        assert request.headers["x-openrouter-metadata"] == "enabled"
        return httpx.Response(
            200,
            json={
                "id": "gen-123",
                "model": "actual/model",
                "openrouter_metadata": {
                    "endpoints": {
                        "available": [
                            {"provider": "Unselected", "selected": False},
                            {"provider": "Inference A", "selected": True},
                        ]
                    }
                },
                "choices": [{"finish_reason": "length", "message": {"content": '{"name":"Jane"}'}}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 6000,
                    "completion_tokens_details": {"reasoning_tokens": 200},
                    "cost": 0.01,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenRouterClient(settings(OPENROUTER_EXTRACTION_PROVIDER="inference-a"), http_client)
        args = (DiagnosticResponse, "extraction", "Return JSON.", {}, {}, "test")
        result, records = await client.complete(*args, max_output_tokens=6000, reasoning={"effort": "none"})
        _, fallback = await client.complete(
            *args[:4],
            {"fallback_from_model": "extraction-model"},
            args[-1],
            model_override="alternate/model",
        )
    assert result.name == "Jane"
    assert requests[0]["provider"] == {
        "require_parameters": True,
        "only": ["inference-a"],
        "allow_fallbacks": False,
    }
    assert requests[0]["reasoning"] == {"effort": "none"}
    assert requests[0]["max_tokens"] == 6000
    assert requests[1]["provider"] == {"require_parameters": True}
    assert records[0].model == "extraction-model" and records[0].response_model == "actual/model"
    assert records[0].requested_provider == "inference-a" and records[0].inference_provider == "Inference A"
    assert records[0].reasoning_tokens == 200 and records[0].max_output_tokens == 6000
    assert records[0].success and records[0].finish_reason == "length" and records[0].cost == 0.01
    assert fallback[0].fallback_from_model == "extraction-model"


@pytest.mark.asyncio
async def test_choice_level_provider_error_records_safe_zero_token_diagnostics() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "error": {
                            "code": 502,
                            "message": "private upstream-provider detail",
                            "metadata": {"provider_name": "example"},
                        }
                    }
                ]
            },
            request=request,
        )

    error, emitted = await complete(handler)

    assert len(error.usage) == len(emitted) == 1
    assert_zero_token_diagnostics(
        error,
        emitted[0],
        error_code="OPENROUTER_PROVIDER_ERROR",
        http_status=200,
        exception_type="OpenRouterError",
        response_received=True,
        response_body_received=True,
    )
    assert "private upstream-provider detail" not in str(error)


@pytest.mark.asyncio
async def test_schema_mismatch_records_safe_zero_token_diagnostics() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"wrong":"shape"}'}}]},
            request=request,
        )

    error, emitted = await complete(handler)

    assert len(error.usage) == len(emitted) == 1
    assert_zero_token_diagnostics(
        error,
        emitted[0],
        error_code="OPENROUTER_SCHEMA_ERROR",
        http_status=200,
        exception_type="ValidationError",
        response_received=True,
        response_body_received=True,
    )


@pytest.mark.asyncio
async def test_transport_failure_records_safe_zero_token_diagnostics() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private socket detail", request=request)

    error, emitted = await complete(handler)

    assert len(error.usage) == len(emitted) == 1
    assert_zero_token_diagnostics(
        error,
        emitted[0],
        error_code="OPENROUTER_TRANSPORT_ERROR",
        http_status=None,
        exception_type="ConnectError",
        response_received=False,
        response_body_received=False,
        request_sent=False,
    )
    assert "private socket detail" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError])
async def test_uncertain_paid_request_is_not_retried(failure_type):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise failure_type("private acknowledgement detail", request=request)

    error, emitted = await complete(handler, retries=2)
    assert calls == len(emitted) == 1
    assert error.transient is True
    assert error.response_uncertain is True
    assert error.retryable is False
    assert emitted[0].request_sent is True
    assert "private acknowledgement detail" not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_unsent_connection_attempts_are_bounded_and_identified(failure_type):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise failure_type("unavailable", request=request)

    error, emitted = await complete(handler, retries=2)
    assert calls == len(emitted) == 3
    assert error.transient is True
    assert error.response_uncertain is False
    assert all(record.request_sent is False for record in emitted)


@pytest.mark.asyncio
async def test_long_retry_after_returns_to_durable_worker_without_early_retry():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "90"}, json={"error": {"code": 429}})

    error, emitted = await complete(handler, retries=2)
    assert calls == len(emitted) == 1
    assert error.transient is True
    assert error.retry_after_seconds == 90


@pytest.mark.asyncio
async def test_bounded_500_retries_retain_last_http_status() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            500,
            json={"error": {"code": 500, "message": f"private attempt {attempts}"}},
            request=request,
        )

    error, emitted = await complete(handler, retries=1)

    assert attempts == 2
    assert len(error.usage) == len(emitted) == 2
    assert [record.retry_attempt for record in emitted] == [0, 1]
    assert all(record.error_code == "OPENROUTER_HTTP_ERROR" for record in emitted)
    assert all(record.http_status == 500 for record in emitted)
    assert all(record.request_sent for record in emitted)
    assert all(record.response_received for record in emitted)
    assert all(record.response_body_received for record in emitted)
    assert error.error_code == "OPENROUTER_HTTP_ERROR"
    assert error.http_status == 500
    assert error.retryable is True
    assert "private attempt" not in str(error)


@pytest.mark.asyncio
async def test_source_advisor_validate_empty_input_skips_provider_request() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"decisions": []})}}]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        advisor = SourceAdvisor(OpenRouterClient(settings(), client=http_client, sleep=no_sleep))
        decisions, usage = await advisor.validate(
            PersonSeed(full_name="Jane Doe"),
            [],
            {"job_id": "job-1", "person_id": "person-1"},
        )

    assert decisions == []
    assert usage == []
    assert advisor.last_usage == []
    assert calls == 0
