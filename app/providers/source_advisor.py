"""Role-A source planning and validation wrapper."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from app.prompts.source_advisor import (
    SOURCE_ADVISOR_PROMPT_VERSION,
    SOURCE_ADVISOR_SYSTEM_PROMPT,
)
from app.providers.openrouter import AttemptCallback, OpenRouterClient, UsageCallback
from app.schemas import Contract, PersonSeed, SourceCandidate, SourceType, UsageRecord

MAX_USAGE_RECORDS = 100


class QueryPlan(Contract):
    queries: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("queries")
    @classmethod
    def clean_queries(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for query in value:
            normalized = " ".join(str(query).split())[:600]
            key = normalized.casefold()
            if normalized and key not in seen:
                cleaned.append(normalized)
                seen.add(key)
        return cleaned


class SourceDecision(Contract):
    candidate_id: str
    source_type: SourceType = SourceType.unknown
    relevance: Literal["likely", "ambiguous", "unrelated"] = "ambiguous"
    duplicate_of: str | None = None
    identity_clues: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("identity_clues")
    @classmethod
    def limit_identity_clues(cls, value: list[str]) -> list[str]:
        return [" ".join(str(clue).split())[:160] for clue in value[:6] if str(clue).strip()]


class SourceDecisionSet(Contract):
    decisions: list[SourceDecision] = Field(default_factory=list, max_length=50)


class SourceAdvisor:
    """Thin wrapper around the source role model."""

    def __init__(
        self,
        client: OpenRouterClient,
        *,
        max_queries: int | None = None,
        prompt: str = SOURCE_ADVISOR_SYSTEM_PROMPT,
        prompt_version: str = SOURCE_ADVISOR_PROMPT_VERSION,
    ) -> None:
        self.client = client
        self.max_queries = max_queries
        self.prompt = prompt
        self.prompt_version = prompt_version
        self.last_usage: list[UsageRecord] = []
        self.usage: list[UsageRecord] = []

    async def plan(
        self,
        seed: PersonSeed,
        known_clues: list[str],
        context: dict,
        *,
        before_attempt: AttemptCallback | None = None,
        on_usage: UsageCallback | None = None,
    ) -> tuple[list[str], list[UsageRecord]]:
        response, usage = await self.client.complete(
            QueryPlan,
            "source",
            self.prompt,
            {
                "task": "plan_search_queries",
                "seed": seed.model_dump(mode="json"),
                "known_clues": known_clues[:12],
            },
            context,
            self.prompt_version,
            before_attempt=before_attempt,
            on_usage=on_usage,
        )
        self._record_usage(usage)
        limit = self.max_queries or int(context.get("max_queries") or len(response.queries))
        return response.queries[: max(0, limit)], usage

    async def validate(
        self,
        seed: PersonSeed,
        candidates: list[SourceCandidate],
        context: dict,
        *,
        before_attempt: AttemptCallback | None = None,
        on_usage: UsageCallback | None = None,
    ) -> tuple[list[SourceDecision], list[UsageRecord]]:
        candidate_ids = {candidate.candidate_id for candidate in candidates}
        response, usage = await self.client.complete(
            SourceDecisionSet,
            "source",
            self.prompt,
            {
                "task": "validate_candidates",
                "seed": seed.model_dump(mode="json"),
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "url": candidate.url,
                        "title": candidate.title,
                        "snippet": candidate.snippet,
                        "domain": candidate.domain,
                    }
                    for candidate in candidates
                ],
            },
            context,
            self.prompt_version,
            before_attempt=before_attempt,
            on_usage=on_usage,
        )
        self._record_usage(usage)

        by_id: dict[str, SourceDecision] = {}
        for decision in response.decisions:
            if decision.candidate_id not in candidate_ids:
                continue
            if decision.duplicate_of is not None and decision.duplicate_of not in candidate_ids:
                decision.duplicate_of = None
            by_id[decision.candidate_id] = decision

        for candidate_id in candidate_ids - set(by_id):
            by_id[candidate_id] = SourceDecision(candidate_id=candidate_id)
        return [by_id[candidate.candidate_id] for candidate in candidates], usage

    def _record_usage(self, usage: list[UsageRecord]) -> None:
        self.last_usage = usage
        self.usage.extend(usage)
        if len(self.usage) > MAX_USAGE_RECORDS:
            self.usage = self.usage[-MAX_USAGE_RECORDS:]
