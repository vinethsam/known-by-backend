"""One compact performance event per person; no input text or new database rows."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

from app.schemas import (
    JobResults,
    ProfileField,
    ResearchMetrics,
    ResearchResult,
    UsageRecord,
    has_researched_value,
)

logger = logging.getLogger(__name__)
PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
PERSISTENCE_FAILED_OUTCOME = "persistence_failed"
TIMINGS = (
    "queue_wait_ms",
    "discovery_ms",
    "source_validation_ms",
    "retrieval_ms",
    "processing_ms",
    "extraction_ms",
    "reconciliation_ms",
    "persistence_ms",
    "total_person_ms",
)
COUNTERS = (
    "search_queries",
    "search_tool_calls",
    "candidates_discovered",
    "candidates_selected",
    "deterministic_validations",
    "sources_requested",
    "sources_retrieved",
    "retrieval_cache_hits",
    "retrieval_cache_misses",
    "extraction_cache_hits",
    "request_cache_hits",
    "static_fetches",
    "playwright_fallbacks",
    "extraction_calls",
    "extraction_chunks",
    "input_tokens",
    "output_tokens",
    "provider_cost",
)


@dataclass
class PersonPerformance:
    values: dict[str, float | int] = field(default_factory=lambda: dict.fromkeys((*TIMINGS, *COUNTERS), 0))
    research: dict | None = None
    metrics: ResearchMetrics | None = None


_current: ContextVar[PersonPerformance | None] = ContextVar("person_performance", default=None)


def count(name: str, amount: int | float = 1) -> None:
    current = _current.get()
    if current is not None:
        if name not in COUNTERS:
            raise ValueError("Unknown performance counter")
        current.values[name] += amount
        if current.metrics is not None and name in {"retrieval_cache_hits", "retrieval_cache_misses"}:
            setattr(current.metrics, name, getattr(current.metrics, name) + amount)


def bind_research_metrics(metrics: ResearchMetrics) -> None:
    """Persist cheap retrieval counters through the existing checkpoint state."""
    current = _current.get()
    if current is not None:
        current.metrics = metrics


def _proven_unsent(record: UsageRecord) -> bool:
    return (
        not record.success
        and not record.request_sent
        and record.exception_type in {"ConnectError", "ConnectTimeout", "PoolTimeout"}
    )


def _usage_summary(records: list[UsageRecord], *, unrecorded_calls: int = 0) -> dict:
    reported = sum(record.cost for record in records if record.cost is not None)
    unknown = unrecorded_calls + sum(record.cost is None and not _proven_unsent(record) for record in records)
    return {
        "models": sorted({record.model for record in records}),
        "inference_providers": sorted(
            {record.inference_provider for record in records if record.inference_provider}
        ),
        "model_calls": sum(not _proven_unsent(record) for record in records) + unrecorded_calls,
        "successful_calls": sum(record.success for record in records),
        "output_limit_failures": sum(record.exception_type == "OutputTokenLimit" for record in records),
        "retries": sum(record.retry_attempt > 0 for record in records),
        "fallback_calls": sum(
            record.fallback_from_model is not None and not _proven_unsent(record) for record in records
        ),
        "input_tokens": sum(record.prompt_tokens for record in records),
        "output_tokens": sum(record.completion_tokens for record in records),
        "reported_reasoning_tokens": sum(record.reasoning_tokens or 0 for record in records),
        "reported_api_cost": round(reported, 8),
        "actual_api_cost": None if unknown else round(reported, 8),
        "cost_complete": not unknown,
        "unknown_cost_calls": unknown,
    }


def summarize_person(result: ResearchResult, *, targeted_searches: Iterable[dict] | None = None) -> dict:
    """Cumulative accounting, independent of the number of worker leases.

    Usage IDs are durable and may occur in recovered snapshots more than once.
    Unknown provider cost is not zero. Reserved but unacknowledged attempts also
    make actual cost unknown; proven pre-write failures did not call the model.
    """
    from app.research.normalisation import name_key

    ledger = list({record.usage_id: record for record in result.usage}.values())
    metrics = result.profile.metrics
    operations = {}
    for record in ledger:
        operations.setdefault(record.operation, []).append(record)
    unrecorded = max(0, metrics.llm_calls - sum(not _proven_unsent(record) for record in ledger))
    accounting = _usage_summary(ledger, unrecorded_calls=unrecorded)
    breakdown = {operation: _usage_summary(records) for operation, records in sorted(operations.items())}
    if unrecorded:
        breakdown["unacknowledged"] = _usage_summary([], unrecorded_calls=unrecorded)
    supported = set()
    field_sources = {}
    evidence_ids = set()
    accepted_claim_ids = set()
    by_claim = {claim.claim_id: claim for claim in result.claims}
    source_ids_by_url = {
        url: source.source_id
        for source in result.sources
        for url in (source.canonical_url, source.final_url, source.requested_url)
        if url
    }
    for record in result.profile.records or [result.profile]:
        for field_name in ProfileField:
            decision = record.fields.get(field_name)
            if not has_researched_value(decision):
                continue
            confidence = (
                decision.research_confidence
                if decision.research_confidence is not None
                else decision.confidence
            )
            supporting_claim_ids = set(decision.supporting_claim_ids)
            if (
                field_name == ProfileField.full_name
                and decision.provenance_status == "corroborated"
                and decision.research_confidence
            ):
                supporting_claim_ids.update(
                    claim_id
                    for claim_id in decision.scoring_components.get("seed_corroborating_claim_ids", [])
                    if claim_id in by_claim
                    and by_claim[claim_id].person_id == result.profile.person_id
                    and by_claim[claim_id].field == ProfileField.full_name
                    and name_key(by_claim[claim_id].normalised_value) == name_key(decision.value)
                )
            if confidence <= 0 or not (
                supporting_claim_ids or decision.supporting_source_ids or decision.sources
            ):
                continue
            supported.add(field_name)
            accepted_claim_ids.update(supporting_claim_ids)
            if decision.selected_claim_id:
                accepted_claim_ids.add(decision.selected_claim_id)
            evidence_ids.update(decision.supporting_source_ids)
            evidence_ids.update(source_ids_by_url.get(url, "url:" + url) for url in decision.sources)
            evidence_ids.update(
                by_claim[claim_id].source_id for claim_id in supporting_claim_ids if claim_id in by_claim
            )
            if targeted_searches is not None:
                ids = field_sources.setdefault(field_name.value, set())
                ids.update(decision.supporting_source_ids)
                ids.update(source_ids_by_url[url] for url in decision.sources if url in source_ids_by_url)
                ids.update(
                    by_claim[claim_id].source_id for claim_id in supporting_claim_ids if claim_id in by_claim
                )
                if decision.selected_claim_id in by_claim:
                    ids.add(by_claim[decision.selected_claim_id].source_id)
    searches_with_support = metrics.field_specific_searches_with_support
    if targeted_searches is not None:
        from app.retrieval.urls import canonicalise_url

        requested_urls = {
            source.source_id: canonicalise_url(source.requested_url) for source in result.sources
        }
        searches_with_support = sum(
            bool(
                set(search["urls"])
                & {
                    requested_urls[source_id]
                    for field_name in search["fields"]
                    for source_id in field_sources.get(field_name, ())
                    if source_id in requested_urls
                }
            )
            for search in targeted_searches
        )
    hits, misses = metrics.retrieval_cache_hits, metrics.retrieval_cache_misses
    searches = [
        record for record in ledger if record.operation == "web_search" and not _proven_unsent(record)
    ]
    reported_searches = sum(record.web_search_requests or 0 for record in searches)
    summary = {
        **accounting,
        "operations": breakdown,
        "discovery_requests": metrics.queries_performed,
        "reported_search_requests": reported_searches,
        "actual_search_requests": (
            reported_searches
            if metrics.web_search_calls_reserved <= len(searches)
            and all(record.web_search_requests is not None for record in searches)
            else None
        ),
        "sources_discovered": metrics.sources_discovered,
        "sources_retrieved": sum(200 <= source.fetch_status < 300 for source in result.sources),
        "sources_used_as_evidence": len(evidence_ids),
        "retrieval_cache_hits": hits,
        "retrieval_cache_misses": misses,
        "extraction_cache_hits": metrics.extraction_cache_hits,
        "request_cache_hits": metrics.request_cache_hits,
        "deterministic_claims_extracted": metrics.deterministic_claims_extracted,
        "deterministic_claims_grounded": metrics.deterministic_claims_grounded,
        "deterministic_claims_accepted": sum(
            claim_id in accepted_claim_ids and claim.extraction_model.startswith("deterministic:")
            for claim_id, claim in by_claim.items()
        ),
        "extraction_calls_avoided": metrics.extraction_calls_avoided,
        "chunks_skipped": metrics.chunks_skipped,
        "duplicate_evidence_blocks": metrics.duplicate_evidence_blocks,
        "duplicate_evidence_chars": metrics.duplicate_evidence_chars,
        "llm_extraction_escalations": breakdown.get("extract_claims", {}).get("model_calls", 0),
        "field_specific_searches": metrics.field_specific_searches,
        "field_specific_searches_with_support": searches_with_support,
        "claims_grounded": len(by_claim),
        "claims_selected": len(accepted_claim_ids & by_claim.keys()),
        "source_reuse_rate": round(hits / (hits + misses), 6) if hits + misses else None,
        "independently_supported_fields": [field.value for field in ProfileField if field in supported],
        "unresolved_required_fields": [field.value for field in ProfileField if field not in supported],
        "independent_field_coverage_rate": round(len(supported) / len(ProfileField), 6),
        "successfully_enriched": bool(supported - {ProfileField.full_name, ProfileField.profile_link}),
        "research_outcome": result.profile.research_status,
        "person_status": result.profile.status.value,
        "failure_reason": metrics.failure_reason,
        "review_required": result.profile.review_required,
    }
    return (
        persistence_failure_summary(summary)
        if result.profile.research_status == PERSISTENCE_FAILED_OUTCOME
        else summary
    )


def persistence_failure_summary(summary: dict) -> dict:
    return dict(
        summary,
        successfully_enriched=False,
        research_outcome=PERSISTENCE_FAILED_OUTCOME,
        person_status="failed",
        failure_reason=PERSISTENCE_FAILED,
        review_required=False,
    )


def record_person_result(
    result: ResearchResult, *, outcome: str | None = None, targeted_searches: Iterable[dict] | None = None
) -> dict:
    """Attach final cumulative diagnostics to existing state and the lease event."""
    summary = summarize_person(result, targeted_searches=targeted_searches)
    result.profile.metrics.field_specific_searches_with_support = summary[
        "field_specific_searches_with_support"
    ]
    if outcome == PERSISTENCE_FAILED_OUTCOME:
        summary = persistence_failure_summary(summary)
    elif outcome is not None:
        summary["research_outcome"] = outcome
    result.profile.metrics.accounting = summary
    current = _current.get()
    if current is not None:
        current.research = summary
    return summary


def summarize_job(results: JobResults) -> dict:
    """Aggregate existing results on demand, without historical reads or backfills.

    A profile/checkpoint proves research was attempted; rows rejected before
    research and queued cancellations do not dilute cost or coverage denominators.
    Successfully enriched means at least one independently supported professional
    or education field; name/profile-link-only results are reported separately.
    """
    people = {person.person_id: person for person in results.people}
    summaries = [summarize_person(person.result) for person in people.values() if person.result is not None]
    return aggregate_person_summaries(summaries)


def aggregate_person_summaries(summaries: list[dict], *, unmeasured_people: int = 0) -> dict:
    """Aggregate persisted compact accounting once a job becomes terminal.

    Count attempted people without a summary explicitly. Their unknown work must
    not silently appear free or dilute the independently supported field rate.
    """
    attempted = len(summaries) + unmeasured_people
    enriched = sum(summary["successfully_enriched"] for summary in summaries)
    supported = sum(len(summary["independently_supported_fields"]) for summary in summaries)
    complete = not unmeasured_people and all(summary["cost_complete"] for summary in summaries)
    subtotal = sum(summary["reported_api_cost"] for summary in summaries)
    actual = subtotal if complete else None
    hits = sum(summary["retrieval_cache_hits"] for summary in summaries)
    misses = sum(summary["retrieval_cache_misses"] for summary in summaries)
    calls_by_operation = Counter()
    for summary in summaries:
        calls_by_operation.update(
            {name: usage["model_calls"] for name, usage in summary.get("operations", {}).items()}
        )
    measured_searches = [
        summary for summary in summaries if summary.get("field_specific_searches_with_support") is not None
    ]

    def average(denominator):
        return round(actual / denominator, 8) if actual is not None and denominator else None

    return {
        "attempted_people": attempted,
        "people_measured": len(summaries),
        "unmeasured_people": unmeasured_people,
        "successfully_enriched_people": enriched,
        "independently_supported_fields": supported,
        "actual_api_cost": round(actual, 8) if actual is not None else None,
        "reported_api_cost": round(subtotal, 8),
        "cost_complete": complete,
        "unknown_cost_calls": sum(summary["unknown_cost_calls"] for summary in summaries),
        "model_calls_by_operation": dict(sorted(calls_by_operation.items())),
        "research_outcomes": dict(
            Counter(summary.get("research_outcome", "unknown") for summary in summaries)
        ),
        "identity_unresolved_people": sum(
            summary.get("failure_reason") == "IDENTITY_UNRESOLVED" for summary in summaries
        ),
        "review_required_people": sum(summary.get("review_required", False) for summary in summaries),
        "field_specific_searches_measured": sum(
            summary["field_specific_searches"] for summary in measured_searches
        ),
        "field_specific_searches_with_support": sum(
            summary["field_specific_searches_with_support"] for summary in measured_searches
        ),
        "average_actual_cost_per_attempted_person": average(attempted),
        "average_actual_cost_per_enriched_person": average(enriched),
        "average_actual_cost_per_supported_field": average(supported),
        "average_model_calls_per_person": (
            round(sum(summary["model_calls"] for summary in summaries) / attempted, 6)
            if attempted and not unmeasured_people
            else None
        ),
        "independent_field_coverage_rate": round(supported / (attempted * len(ProfileField)), 6)
        if attempted and not unmeasured_people
        else None,
        "source_reuse_rate": round(hits / (hits + misses), 6)
        if hits + misses and not unmeasured_people
        else None,
        **{
            counter: sum(summary.get(counter, 0) for summary in summaries)
            for counter in (
                "deterministic_claims_extracted",
                "deterministic_claims_grounded",
                "deterministic_claims_accepted",
                "extraction_calls_avoided",
                "chunks_skipped",
                "duplicate_evidence_blocks",
                "duplicate_evidence_chars",
                "llm_extraction_escalations",
                "field_specific_searches",
                "claims_grounded",
                "claims_selected",
            )
        },
    }


@contextmanager
def stage(name: str) -> Iterator[None]:
    current = _current.get()
    if current is None:
        yield
        return
    if name not in TIMINGS:
        raise ValueError("Unknown performance stage")
    started = perf_counter()
    try:
        yield
    finally:
        current.values[name] += (perf_counter() - started) * 1000


@contextmanager
def person_performance(job_id: str, person_id: str, *, queue_wait_ms: float = 0) -> Iterator[None]:
    # The worker owns the outer lifetime including persistence. Direct research
    # callers still receive exactly one event through the nested orchestrator scope.
    if _current.get() is not None:
        yield
        return
    performance = PersonPerformance()
    performance.values["queue_wait_ms"] = max(0, queue_wait_ms)
    token = _current.set(performance)
    started = perf_counter()
    try:
        yield
    finally:
        performance.values["total_person_ms"] = (perf_counter() - started) * 1000
        _current.reset(token)
        logger.info(
            "person_performance",
            extra={
                "job_id": job_id,
                "person_id": person_id,
                "pipeline_stage": "person",
                "performance": {
                    key: round(value, 8 if key == "provider_cost" else 3)
                    if isinstance(value, float)
                    else value
                    for key, value in performance.values.items()
                }
                | ({"research": performance.research} if performance.research is not None else {}),
            },
        )
