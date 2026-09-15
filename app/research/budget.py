"""A per-person adapter fences every paid attempt, including provider retries."""

import json
import logging

from app.config import Settings
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

        async def before_attempt(attempt):
            if self.metrics.llm_calls >= self.settings.MAX_LLM_CALLS_PER_PERSON:
                raise BudgetExceeded("MAX_LLM_CALLS")
            if self.metrics.tokens_budgeted + reserve > self.settings.MAX_TOKENS_PER_PERSON:
                raise BudgetExceeded("TOKEN_BUDGET")
            self.metrics.llm_calls += 1
            self.metrics.tokens_budgeted += reserve
            if self.checkpoint:
                await self.checkpoint()

        async def on_usage(record):
            self.usage.append(record)
            actual = record.prompt_tokens + record.completion_tokens
            self.metrics.tokens_used += actual
            self.metrics.cost += record.cost or 0
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
                    "cost": record.cost,
                    "duration_ms": record.latency_ms,
                },
            )

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
