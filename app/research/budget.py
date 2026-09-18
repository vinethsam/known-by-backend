"""A per-person adapter fences every paid attempt, including provider retries."""

import json
import logging

from app.config import Settings
from app.providers.openrouter import WEB_SEARCH_MAX_CHARACTERS, WEB_SEARCH_MAX_OUTPUT_TOKENS
from app.schemas import ResearchMetrics, UsageRecord

logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    pass


class BudgetedModel:
    def __init__(self, client, settings: Settings, metrics: ResearchMetrics, usage: list[UsageRecord]):
        self.client, self.settings, self.metrics, self.usage = client, settings, metrics, usage
        self.checkpoint = None

    async def complete(self, schema, role, prompt, payload, context, prompt_version, **kwargs):
        # UTF-8 byte count provides a conservative tokenizer-independent input estimate;
        # include the response schema, JSON envelope, and bounded output reservation.
        body = json.dumps(
            {"prompt": prompt, "payload": payload, "context": context, "schema": schema.model_json_schema()},
            ensure_ascii=True,
        )
        reserve = (
            len(body.encode("utf-8"))
            + self.settings.OPENROUTER_MAX_TOKENS
            + self.settings.BUDGET_MESSAGE_OVERHEAD_TOKENS
        )
        before_attempt, on_usage = self._callbacks(reserve)
        return await self.client.complete(
            schema,
            role,
            prompt,
            payload,
            context,
            prompt_version,
            before_attempt=before_attempt,
            on_usage=on_usage,
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
        before_attempt, on_usage = self._callbacks(reserve, search_results=limit)
        return await provider.search(
            query, limit, context=context, before_attempt=before_attempt, on_usage=on_usage
        )

    def _callbacks(self, reserve: int, *, search_results: int = 0):
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
            if search_results:
                self.metrics.web_search_calls_reserved += 1
                self.metrics.search_results_reserved += search_results
            if self.checkpoint:
                await self.checkpoint()

        async def on_usage(record):
            self.usage.append(record)
            actual = record.prompt_tokens + record.completion_tokens
            self.metrics.tokens_used += actual
            self.metrics.cost += record.cost or 0
            self.metrics.web_search_requests += record.web_search_requests or 0
            if record.prompt_tokens > 0 and record.completion_tokens > 0:
                self.metrics.tokens_budgeted += actual - reserve
            if self.checkpoint:
                await self.checkpoint()
            logger.info(
                "model_attempt",
                extra={
                    "job_id": record.job_id,
                    "person_id": record.person_id,
                    "source_id": record.source_id,
                    "pipeline_stage": record.role,
                    "model": record.model,
                    "prompt_version": record.prompt_version,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "web_search_requests": record.web_search_requests,
                    "cost": record.cost,
                    "duration_ms": record.latency_ms,
                },
            )

            if search_results and record.web_search_requests is not None and record.web_search_requests > 1:
                # Fail closed if upstream reports ignoring the request's hard tool cap.
                raise BudgetExceeded("SEARCH_TOOL_LIMIT_VIOLATION")

        return before_attempt, on_usage
