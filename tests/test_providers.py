from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.prompts.extraction import EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT
from app.providers.openrouter import (
    OpenRouterClient,
    OpenRouterError,
    strict_json_schema,
)
from app.providers.source_advisor import SourceAdvisor
from app.schemas import (
    Contract,
    ExtractionResponse,
    PersonSeed,
    SourceCandidate,
    SourceType,
)


async def no_sleep(_: float) -> None:
    return None


def settings(**overrides) -> Settings:
    values = {
        "APP_ENV": "test",
        "OPENROUTER_API_KEY": SecretStr("openrouter-token"),
        "OPENROUTER_SOURCE_MODEL": "source-model",
        "OPENROUTER_EXTRACTION_MODEL": "extraction-model",
        "OPENROUTER_MAX_RETRIES": 1,
    }
    values.update(overrides)
    return Settings(**values)


class TinyResponse(Contract):
    name: str
    optional_note: str | None = None


def openrouter_response(content: str, usage: dict | None = None, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 2, "completion_tokens": 3, "cost": 0.01},
        },
    )


@pytest.mark.asyncio
async def test_openrouter_retries_empty_content_and_returns_all_usage() -> None:
    attempts = 0
    before: list[dict] = []
    emitted = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.headers["authorization"] == "Bearer openrouter-token"
        body = json.loads(request.content)
        assert body["model"] == "source-model"
        assert body["response_format"]["type"] == "json_schema"
        assert body["provider"]["require_parameters"] is True
        assert "tools" not in body and "plugins" not in body and "max_tool_calls" not in body
        if attempts == 1:
            return openrouter_response("", {"prompt_tokens": 5, "completion_tokens": 1})
        return openrouter_response(
            '{"name":"Jane","optional_note":null}', {"prompt_tokens": 7, "completion_tokens": 2}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    openrouter = OpenRouterClient(settings(), client=client, sleep=no_sleep)

    parsed, usage = await openrouter.complete(
        TinyResponse,
        "source",
        "Return JSON.",
        {"x": 1},
        {"job_id": "job-1", "person_id": "person-1"},
        "tiny-v1",
        before_attempt=before.append,
        on_usage=emitted.append,
    )

    assert parsed.name == "Jane"
    assert attempts == 2
    assert [record.success for record in usage] == [False, True]
    assert [record.prompt_tokens for record in usage] == [5, 7]
    assert [item["attempt"] for item in before] == [0, 1]
    assert emitted == usage


@pytest.mark.asyncio
async def test_openrouter_schema_mismatch_raises_with_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return openrouter_response(
            '{"wrong":"shape"}', {"prompt_tokens": 4, "completion_tokens": 2}, request=request
        )

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"wrong":"shape"}'}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
    openrouter = OpenRouterClient(settings(OPENROUTER_MAX_RETRIES=0), client=client, sleep=no_sleep)

    with pytest.raises(OpenRouterError) as exc_info:
        await openrouter.complete(
            TinyResponse,
            "source",
            "Return JSON.",
            {},
            {"job_id": "job-1", "person_id": "person-1"},
            "tiny-v1",
        )

    assert len(exc_info.value.usage) == 1
    assert exc_info.value.usage[0].success is False
    assert exc_info.value.usage[0].completion_tokens == 2


@pytest.mark.asyncio
async def test_source_advisor_returns_bounded_queries_decisions_and_usage() -> None:
    calls = 0
    candidate = SourceCandidate(candidate_id="cand-1", url="https://example.edu/jane", title="Jane Doe")

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        user_payload = json.loads(body["messages"][1]["content"])["payload"]
        if user_payload["task"] == "plan_search_queries":
            return openrouter_response(
                '{"queries":["Jane Doe university","Jane Doe university","Jane Doe profile"]}'
            )
        return openrouter_response(
            '{"decisions":[{"candidate_id":"cand-1","source_type":"university","relevance":"likely",'
            '"duplicate_of":null,"identity_clues":["Jane Doe"]},'
            '{"candidate_id":"invented","source_type":"unknown","relevance":"likely",'
            '"duplicate_of":null,"identity_clues":[]}]}'
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    advisor = SourceAdvisor(OpenRouterClient(settings(), client=client, sleep=no_sleep), max_queries=2)
    seed = PersonSeed(full_name="Jane Doe", university_name="Example University")

    queries, query_usage = await advisor.plan(seed, [], {"job_id": "job-1", "person_id": "person-1"})
    decisions, decision_usage = await advisor.validate(
        seed, [candidate], {"job_id": "job-1", "person_id": "person-1"}
    )

    assert queries == ["Jane Doe university", "Jane Doe profile"]
    assert decisions[0].candidate_id == "cand-1"
    assert decisions[0].source_type == SourceType.university
    assert len(query_usage) == 1
    assert len(decision_usage) == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_openrouter_extraction_response_schema() -> None:
    content = json.dumps(
        {
            "claims": [
                {
                    "field": "full_name",
                    "value": "Jane Doe",
                    "evidence": "Jane Doe is listed as a fellow.",
                    "evidence_location": None,
                    "temporal_context": None,
                    "as_of_date": None,
                    "end_date": None,
                    "is_current": None,
                    "directness": "explicit",
                    "source_claim_type": "statement",
                    "fact_group": None,
                    "subject_name": "Jane Doe",
                }
            ]
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "extraction-model"
        assert "tools" not in body and "max_tool_calls" not in body
        return openrouter_response(content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    openrouter = OpenRouterClient(settings(), client=client, sleep=no_sleep)

    parsed, usage = await openrouter.complete(
        ExtractionResponse,
        "extraction",
        EXTRACTION_SYSTEM_PROMPT,
        {"source_text": "Jane Doe is listed as a fellow."},
        {"job_id": "job-1", "person_id": "person-1", "source_id": "source-1"},
        EXTRACTION_PROMPT_VERSION,
    )

    assert parsed.claims[0].value == "Jane Doe"
    assert usage[0].role == "extraction"
    assert usage[0].source_id == "source-1"


def test_strict_json_schema_marks_nested_objects_strict() -> None:
    schema = strict_json_schema(TinyResponse)

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"name", "optional_note"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [b"\xff", b"{", b"[]", b'{"choices":{"bad":1}}', b'{"choices":[{"message":[]}]}']
)
async def test_openrouter_malformed_envelopes_record_each_paid_attempt(body):
    emitted = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as client:
        with pytest.raises(OpenRouterError) as error:
            await OpenRouterClient(settings(), client, sleep=no_sleep).complete(
                TinyResponse,
                "source",
                "JSON",
                {},
                {"job_id": "job", "person_id": "person"},
                "v1",
                on_usage=emitted.append,
            )
    assert len(error.value.usage) == len(emitted) == 2
    assert all(not u.success for u in emitted)
    assert len({u.usage_id for u in emitted}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,cost", [("invalid", "NaN"), (-4, -1), (True, {}), (1.5, "Infinity")])
async def test_invalid_usage_does_not_erase_successful_paid_attempt(tokens, cost):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: openrouter_response(
                '{"name":"Jane"}', {"prompt_tokens": tokens, "completion_tokens": tokens, "cost": cost}
            )
        )
    ) as client:
        parsed, usage = await OpenRouterClient(settings(), client).complete(
            TinyResponse, "source", "JSON", {}, {"job_id": "job", "person_id": "person"}, "v1"
        )
    assert parsed.name == "Jane"
    assert len(usage) == 1 and usage[0].success
    assert usage[0].prompt_tokens == usage[0].completion_tokens == 0
    assert usage[0].cost is None


@pytest.mark.asyncio
async def test_partial_usage_retains_reservation_and_checkpoints_attempt():
    from app.research.budget import BudgetedModel
    from app.schemas import ResearchMetrics

    metrics, ledger, checkpoints = ResearchMetrics(), [], []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: openrouter_response('{"name":"Jane"}', {"completion_tokens": 3})
        )
    ) as client:
        budgeted = BudgetedModel(OpenRouterClient(settings(), client), settings(), metrics, ledger)

        async def checkpoint():
            checkpoints.append((metrics.llm_calls, metrics.tokens_budgeted, len(ledger)))

        budgeted.checkpoint = checkpoint
        await budgeted.complete(
            TinyResponse, "source", "JSON", {}, {"job_id": "job", "person_id": "person"}, "v1"
        )
    assert len(checkpoints) == 2
    assert checkpoints[0][0] == 1 and checkpoints[0][2] == 0
    assert checkpoints[1][2] == 1
    assert checkpoints[0][1] == checkpoints[1][1] > 3
