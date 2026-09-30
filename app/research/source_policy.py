"""Remove now-forbidden evidence from retained results at the read boundary."""

from app.config import ScoringPolicy
from app.research.reconciliation import reconcile
from app.retrieval.urls import source_policy_allows, source_record_allowed
from app.schemas import PersonSeed, ProfileField, ResearchResult


def sanitize_research_result(
    result: ResearchResult, seed: PersonSeed, policy: ScoringPolicy
) -> ResearchResult:
    """Reconcile only when legacy evidence violates the current source policy.

    Historical jobs can predate a hard source exclusion. Merely hiding their URLs
    would retain values and confidence derived from those sources. Reconciliation
    reuses retained evidence and the original seed without retrieval or model calls.
    """
    sources = [source for source in result.sources if source_record_allowed(source)]
    allowed_ids = {source.source_id for source in sources}
    claims = [
        claim
        for claim in result.claims
        if claim.source_id in allowed_ids
        and (claim.field != ProfileField.profile_link or source_policy_allows(claim.normalised_value))
    ]
    decisions = [result.profile.fields, *(record.fields for record in result.profile.records)]
    blocked_decision = any(
        any(not source_policy_allows(url) for url in decision.sources)
        or (
            field == ProfileField.profile_link and decision.value and not source_policy_allows(decision.value)
        )
        for fields in decisions
        for field, decision in fields.items()
    )
    if len(sources) == len(result.sources) and len(claims) == len(result.claims) and not blocked_decision:
        return result
    profile = reconcile(result.profile.person_id, seed, claims, sources, policy)
    profile.started_at = result.profile.started_at
    profile.completed_at = result.profile.completed_at
    profile.metrics = result.profile.metrics.model_copy(deep=True)
    profile.sources_considered = result.profile.sources_considered
    profile.research_status = (
        result.profile.research_status
        if result.profile.research_status in {"researching", "retryable_research_failure"}
        else "insufficient_evidence"
        if not profile.coverage
        else "needs_review"
        if profile.review_required
        else "clean"
    )
    return result.model_copy(update={"profile": profile, "sources": sources, "claims": claims})
