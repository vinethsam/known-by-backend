"""A per-person adapter fences every paid attempt, including provider retries."""

import asyncio
import hashlib
import json
import logging

from app.config import Settings
from app.providers.openrouter import WEB_SEARCH_MAX_CHARACTERS, WEB_SEARCH_MAX_OUTPUT_TOKENS
from app.research.telemetry import count
from app.schemas import ResearchMetrics, SourceCandidate, UsageRecord

logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


class BudgetedModel:
    def __init__(self, client, settings: Settings, metrics: ResearchMetrics, usage: list[UsageRecord]):
        self.client, self.settings, self.metrics, self.usage = client, settings, metrics, usage
        self.checkpoint = None
        self._budget_lock = asyncio.Lock()
        self._requests = self.metrics.recovery.setdefault("requests", {})

    def _key(self, operation):
        return hashlib.sha256(json.dumps(operation, sort_keys=True, ensure_ascii=True).encode()).hexdigest()

    async def _request(self, key, call, decode, encode):
        if self.checkpoint is None:
            return await call()
        receipt = self._requests.get(key)
        if receipt:
            if receipt.get("state") == "completed":
                return decode(receipt["result"])
            if receipt.get("state") != "retryable":
                # A crash or lost acknowledgement left no durable reply. Never
                # blindly repeat a request that may already have incurred a charge.
                raise BudgetExceeded("PROVIDER_RESPONSE_UNCERTAIN")
        self._requests[key] = {"state": "in_flight"}
        try:
            value = await call()
        except Exception as exc:
            paid_reply_without_result = self._requests.get(key, {}).get("acknowledged", False)
            if not getattr(exc, "response_uncertain", False) and not paid_reply_without_result:
                self._requests.pop(key, None)
            if self.checkpoint:
                await self.checkpoint()
            raise
        self._requests[key] = {"state": "completed", "result": encode(value)}
        if self.checkpoint:
            await self.checkpoint()
        return value

    def _reservation(self, schema, prompt, payload, context):
        # UTF-8 byte count provides a conservative tokenizer-independent input estimate;
        # include the response schema, JSON envelope, and bounded output reservation.
        body = json.dumps(
            {"prompt": prompt, "payload": payload, "context": context, "schema": schema.model_json_schema()},
            ensure_ascii=True,
        )
        return (
            len(body.encode("utf-8"))
            + self.settings.OPENROUTER_MAX_TOKENS
            + self.settings.BUDGET_MESSAGE_OVERHEAD_TOKENS
        )

    def can_parallel_complete(self, schema, prompt, payloads, context):
        attempts = self.settings.OPENROUTER_MAX_RETRIES + 1
        return (
            self.metrics.llm_calls + len(payloads) * attempts <= self.settings.MAX_LLM_CALLS_PER_PERSON
            and self.metrics.tokens_budgeted
            + sum(self._reservation(schema, prompt, payload, context) for payload in payloads) * attempts
            <= self.settings.MAX_TOKENS_PER_PERSON
        )

    async def complete(self, schema, role, prompt, payload, context, prompt_version, **kwargs):
        reserve = self._reservation(schema, prompt, payload, context)
        key = self._key(
            ["complete", role, prompt_version, prompt, schema.model_json_schema(), payload, context]
        )
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
        )

    def _callbacks(self, reserve: int, *, search_results: int = 0, request_key=None):
        async def before_attempt(attempt):
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
            if request_key and record.success:
                self._requests[request_key]["acknowledged"] = True
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
                    "prompt_version": record.prompt_version,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "web_search_requests": record.web_search_requests,
                    "cost": record.cost,
                    "duration_ms": record.latency_ms,
                    "success": record.success,
                    "error_code": record.error_code,
                    "http_status": record.http_status,
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
