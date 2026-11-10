"""A per-person adapter fences every paid attempt, including provider retries."""

import asyncio
import hashlib
import json
import logging

from app.config import Settings
from app.prompts.extraction import DEEPSEEK_EXTRACTION_COMPACT_OUTPUT
from app.providers.openrouter import (
    OPENROUTER_HTTP_ERROR,
    OPENROUTER_SCHEMA_ERROR,
    WEB_SEARCH_MAX_CHARACTERS,
    WEB_SEARCH_MAX_OUTPUT_TOKENS,
    OpenRouterError,
    account_access_denied,
)
from app.providers.search import SearchProviderError
from app.research.telemetry import count
from app.schemas import ResearchMetrics, SourceCandidate, UsageRecord

logger = logging.getLogger(__name__)

DEEPSEEK_EXTRACTION_MODEL = "deepseek/deepseek-v4.1-flash-1017"
LUNA_EXTRACTION_FALLBACK = "openai/gpt-5.6-luna"


class BudgetExceeded(RuntimeError):
    pass


class BudgetedModel:
    def __init__(self, client, settings: Settings, metrics: ResearchMetrics, usage: list[UsageRecord]):
        self.client, self.settings, self.metrics, self.usage = client, settings, metrics, usage
        self.checkpoint = None
        self._budget_lock = asyncio.Lock()
        self._requests = self.metrics.recovery.setdefault("requests", {})
        self.provider_unavailable = False
        self.provider_unavailable_status = None
        self.model_provider_errors: dict[str, UsageRecord] = {}
        for record in usage:
            self._record_provider_access(record)

    def _record_provider_access(self, record: UsageRecord) -> None:
        status = record.effective_provider_status
        category = record.provider_error_category
        if not record.success and account_access_denied(status, category):
            self.provider_unavailable = True
            self.provider_unavailable_status = status
        elif category in {"route_restricted", "provider_access"}:
            self.model_provider_errors[record.model] = record

    def _key(self, operation):
        return hashlib.sha256(json.dumps(operation, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    async def _request(self, key, call, decode, encode, *, access_error=OpenRouterError):
        # This client is scoped to one person and its fixed model configuration.
        # Reuse exact completed receipts even for callers without persistence.
        # Keep existing receipt hashes so recovering older in-flight requests
        # cannot accidentally issue a second paid request.
        receipt = self._requests.get(key)
        if receipt:
            if receipt.get("state") == "completed":
                self.metrics.request_cache_hits += 1
                count("request_cache_hits")
                return decode(receipt["result"])
            if receipt.get("state") == "output_limited":
                raise OpenRouterError(
                    "OpenRouter structured output exceeded the response token limit",
                    error_code=OPENROUTER_SCHEMA_ERROR,
                    http_status=receipt.get("http_status"),
                    operation=receipt.get("operation"),
                    exception_type="OutputTokenLimit",
                    request_sent=True,
                    response_received=True,
                    response_body_received=True,
                )
            if receipt.get("state") == "access_denied":
                raise access_error(
                    "OpenRouter rejected access for this request",
                    error_code=receipt.get("error_code") or OPENROUTER_HTTP_ERROR,
                    http_status=receipt.get("http_status"),
                    provider_error_category=receipt.get("category"),
                )
            if receipt.get("state") != "retryable":
                # A crash or lost acknowledgement left no durable reply. Never
                # blindly repeat a request that may already have incurred a charge.
                raise BudgetExceeded("PROVIDER_RESPONSE_UNCERTAIN")
        if self.provider_unavailable:
            raise BudgetExceeded("PROVIDER_UNAVAILABLE")
        self._requests[key] = {"state": "in_flight"}
        try:
            value = await call()
        except Exception as exc:
            status = getattr(exc, "http_status", None)
            if account_access_denied(status, getattr(exc, "provider_error_category", None)):
                self.provider_unavailable = True
                self.provider_unavailable_status = status
            receipt = self._requests.get(key, {})
            retained_reply = receipt.get("acknowledged") or receipt.get("state") in {
                "output_limited",
                "access_denied",
            }
            if not getattr(exc, "response_uncertain", False) and not retained_reply:
                self._requests.pop(key, None)
            if self.checkpoint:
                await self.checkpoint()
            raise
        self._requests[key] = {"state": "completed", "result": encode(value)}
        if self.checkpoint:
            await self.checkpoint()
        return value

    def _reservation(self, schema, prompt, payload, context, max_output_tokens=None):
        # UTF-8 byte count provides a conservative tokenizer-independent input estimate;
        # include the response schema, JSON envelope, and bounded output reservation.
        body = json.dumps(
            {"prompt": prompt, "payload": payload, "context": context, "schema": schema.model_json_schema()},
            ensure_ascii=True,
        )
        return (
            len(body.encode("utf-8"))
            + (max_output_tokens or self.settings.OPENROUTER_MAX_TOKENS)
            + self.settings.BUDGET_MESSAGE_OVERHEAD_TOKENS
        )

    def can_parallel_complete(self, schema, prompt, payloads, context):
        attempts = self.settings.OPENROUTER_MAX_RETRIES + 1
        calls = len(payloads)
        reserved = sum(self._reservation(schema, prompt, payload, context) for payload in payloads)
        if self.settings.OPENROUTER_EXTRACTION_MODEL == DEEPSEEK_EXTRACTION_MODEL:
            # Reserve for both outcomes so a speculative later chunk cannot
            # crowd out an earlier chunk's bounded fallback.
            calls *= 2
            fallback_context = dict(context, fallback_from_model=DEEPSEEK_EXTRACTION_MODEL)
            reserved = sum(
                self._reservation(
                    schema,
                    prompt + DEEPSEEK_EXTRACTION_COMPACT_OUTPUT,
                    payload,
                    context,
                    self.settings.OPENROUTER_DEEPSEEK_EXTRACTION_MAX_TOKENS,
                )
                + self._reservation(schema, prompt, payload, fallback_context)
                for payload in payloads
            )
        return (
            self.metrics.llm_calls + calls * attempts <= self.settings.MAX_LLM_CALLS_PER_PERSON
            and self.metrics.tokens_budgeted + reserved * attempts <= self.settings.MAX_TOKENS_PER_PERSON
        )

    async def complete(self, schema, role, prompt, payload, context, prompt_version, **kwargs):
        key = self._key(
            ["complete", role, prompt_version, prompt, schema.model_json_schema(), payload, context]
        )
        if role != "extraction" or self.settings.OPENROUTER_EXTRACTION_MODEL != DEEPSEEK_EXTRACTION_MODEL:
            return await self._complete_attempt(key, schema, role, prompt, payload, context, prompt_version)

        # Keep the logical request key stable for previously completed receipts.
        # Only the provider wire instructions and output allowance change.
        try:
            return await self._complete_attempt(
                key,
                schema,
                role,
                prompt + DEEPSEEK_EXTRACTION_COMPACT_OUTPUT,
                payload,
                context,
                f"{prompt_version}-deepseek-compact",
                max_output_tokens=self.settings.OPENROUTER_DEEPSEEK_EXTRACTION_MAX_TOKENS,
                reasoning={"enabled": self.settings.OPENROUTER_DEEPSEEK_EXTRACTION_REASONING},
            )
        except OpenRouterError as error:
            if error.exception_type != "OutputTokenLimit" and getattr(
                error, "provider_error_category", None
            ) not in {"route_restricted", "provider_access"}:
                raise
            fallback_context = dict(context, fallback_from_model=DEEPSEEK_EXTRACTION_MODEL)
            response, usage = await self._complete_attempt(
                self._key(["extraction_fallback", key, LUNA_EXTRACTION_FALLBACK]),
                schema,
                role,
                prompt,
                payload,
                fallback_context,
                prompt_version,
                model_override=LUNA_EXTRACTION_FALLBACK,
            )
            return response, [*error.usage, *usage]

    async def _complete_attempt(self, key, schema, role, prompt, payload, context, prompt_version, **options):
        reserve = self._reservation(schema, prompt, payload, context, options.get("max_output_tokens"))
        before_attempt, on_usage = self._callbacks(reserve, request_key=key)
        return await self._request(
            key,
            lambda: self.client.complete(
                schema,
                role,
                prompt,
                payload,
                context,
                prompt_version,
                before_attempt=before_attempt,
                on_usage=on_usage,
                **options,
            ),
            lambda data: (schema.model_validate(data[0]), [UsageRecord.model_validate(x) for x in data[1]]),
            lambda value: [value[0].model_dump(mode="json"), [x.model_dump(mode="json") for x in value[1]]],
        )

    async def search(self, provider, query, limit, context):
        # The server may make an initial model pass plus one pass over search results.
        # Reserve bounded result text as well as both model passes before paid work.
        envelope = len(json.dumps({"query": query, "context": context}, ensure_ascii=True).encode())
        reserve = 2 * (
            envelope
            + self.settings.BUDGET_MESSAGE_OVERHEAD_TOKENS
            + min(self.settings.OPENROUTER_MAX_TOKENS, WEB_SEARCH_MAX_OUTPUT_TOKENS)
        ) + limit * (4 * WEB_SEARCH_MAX_CHARACTERS + 1024)
        key = self._key(["search", query, limit, context])
        before_attempt, on_usage = self._callbacks(reserve, search_results=limit, request_key=key)
        return await self._request(
            key,
            lambda: provider.search(
                query, limit, context=context, before_attempt=before_attempt, on_usage=on_usage
            ),
            lambda data: [SourceCandidate.model_validate(x) for x in data],
            lambda value: [x.model_dump(mode="json") for x in value],
            access_error=SearchProviderError,
        )

    def _callbacks(self, reserve: int, *, search_results: int = 0, request_key=None):
        async def before_attempt(attempt):
            if self.provider_unavailable:
                raise BudgetExceeded("PROVIDER_UNAVAILABLE")
            if (denial := self.model_provider_errors.get(attempt.get("model"))) is not None:
                raise OpenRouterError(
                    "OpenRouter access to the requested model is unavailable",
                    error_code=denial.error_code or OPENROUTER_HTTP_ERROR,
                    http_status=denial.effective_provider_status,
                    provider_error_category=denial.provider_error_category,
                    operation=denial.operation,
                )
            if search_results:
                if self.metrics.web_search_calls_reserved >= self.settings.MAX_SOURCE_MODEL_TOOL_CALLS:
                    raise BudgetExceeded("MAX_SEARCH_TOOL_CALLS")
                if (
                    self.metrics.search_results_reserved + search_results
                    > self.settings.MAX_TOTAL_SEARCH_RESULTS_PER_PERSON
                ):
                    raise BudgetExceeded("MAX_SEARCH_RESULTS")
            if self.metrics.llm_calls >= self.settings.MAX_LLM_CALLS_PER_PERSON:
                raise BudgetExceeded("MAX_LLM_CALLS")
            if self.metrics.tokens_budgeted + reserve > self.settings.MAX_TOKENS_PER_PERSON:
                raise BudgetExceeded("TOKEN_BUDGET")
            self.metrics.llm_calls += 1
            self.metrics.tokens_budgeted += reserve
            if attempt.get("role") == "extraction":
                count("extraction_calls")
            if search_results:
                count("search_tool_calls")
                self.metrics.web_search_calls_reserved += 1
                self.metrics.search_results_reserved += search_results
            if request_key:
                self._requests[request_key] = {"state": "in_flight"}
            if self.checkpoint:
                await self.checkpoint()

        async def on_usage(record):
            self.usage.append(record)
            self._record_provider_access(record)
            if request_key and record.success:
                self._requests[request_key]["acknowledged"] = True
            if request_key and record.provider_error_category is not None:
                self._requests[request_key] = {
                    "state": "access_denied",
                    "error_code": record.error_code,
                    "http_status": record.effective_provider_status,
                    "category": record.provider_error_category,
                }
            if request_key and record.exception_type == "OutputTokenLimit":
                self._requests[request_key] = {
                    "state": "output_limited",
                    "http_status": record.http_status,
                    "operation": record.operation,
                }
            actual = record.prompt_tokens + record.completion_tokens
            self.metrics.tokens_used += actual
            self.metrics.cost += record.cost or 0
            count("input_tokens", record.prompt_tokens)
            count("output_tokens", record.completion_tokens)
            count("provider_cost", record.cost or 0)
            self.metrics.web_search_requests += record.web_search_requests or 0
            if record.prompt_tokens > 0 and record.completion_tokens > 0:
                self.metrics.tokens_budgeted += actual - reserve
            proven_unsent = (
                not record.success
                and not record.request_sent
                and record.exception_type in {"ConnectError", "ConnectTimeout", "PoolTimeout"}
            )
            if proven_unsent:
                # Only proven pre-write failures release reservations. A missing
                # acknowledgement or a provider response remains accounted for.
                self.metrics.llm_calls -= 1
                self.metrics.tokens_budgeted -= reserve
                if search_results:
                    self.metrics.web_search_calls_reserved -= 1
                    self.metrics.search_results_reserved -= search_results
            if (
                request_key
                and not record.success
                and (
                    proven_unsent
                    or (
                        record.response_received
                        and record.response_body_received
                        and record.http_status is not None
                        and (record.http_status in {408, 429} or record.http_status >= 500)
                    )
                )
            ):
                # Commit definitive failed-attempt state with its accounting,
                # closing the crash window before the outer request handler.
                self._requests[request_key] = {"state": "retryable"}
            if self.checkpoint:
                await self.checkpoint()
            log = logger.info if record.success else logger.warning
            log(
                "model_attempt",
                extra={
                    "job_id": record.job_id,
                    "person_id": record.person_id,
                    "source_id": record.source_id,
                    "pipeline_stage": record.role,
                    "operation": record.operation,
                    "provider": "openrouter",
                    "model": record.model,
                    "response_model": record.response_model,
                    "requested_provider": record.requested_provider,
                    "inference_provider": record.inference_provider,
                    "fallback_from_model": record.fallback_from_model,
                    "prompt_version": record.prompt_version,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "max_output_tokens": record.max_output_tokens,
                    "reasoning_tokens": record.reasoning_tokens,
                    "finish_reason": record.finish_reason,
                    "web_search_requests": record.web_search_requests,
                    "cost": record.cost,
                    "duration_ms": record.latency_ms,
                    "success": record.success,
                    "error_code": record.error_code,
                    "http_status": record.http_status,
                    "provider_request_id": record.provider_request_id,
                    "provider_error_status": record.provider_error_status,
                    "provider_error_type": record.provider_error_type,
                    "provider_error_category": record.provider_error_category,
                    "provider_limit_source": record.provider_limit_source,
                    "exception_type": record.exception_type,
                    "retry_attempt": record.retry_attempt,
                    "request_sent": record.request_sent,
                    "response_received": record.response_received,
                    "response_body_received": record.response_body_received,
                },
            )

            if search_results and record.web_search_requests is not None and record.web_search_requests > 1:
                # Fail closed if upstream reports ignoring the request's hard tool cap.
                raise BudgetExceeded("SEARCH_TOOL_LIMIT_VIOLATION")

        async def reserve_locked(attempt):
            async with self._budget_lock:
                await before_attempt(attempt)

        async def usage_locked(record):
            async with self._budget_lock:
                await on_usage(record)

        return reserve_locked, usage_locked
