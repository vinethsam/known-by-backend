from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.providers.openrouter import OpenRouterClient, OpenRouterError, strict_json_schema
from app.providers.source_advisor import SourceAdvisor, SourceDecisionSet
from app.schemas import Contract, PersonSeed, UsageRecord


class DiagnosticResponse(Contract):
    name: str


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
) -> None:
    assert error.error_code == error_code
    assert error.http_status == http_status
    assert usage.operation == "validate_candidates"
    assert usage.error_code == error_code
    assert usage.http_status == http_status
    assert usage.exception_type == exception_type
    assert usage.retry_attempt == 0
    assert usage.request_sent is True
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
    )
    assert "private socket detail" not in str(error)


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
