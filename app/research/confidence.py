"""Deterministic evidence scoring; scores are heuristics, not calibrated probabilities."""

from __future__ import annotations

from datetime import date

from app.config import ScoringPolicy
from app.schemas import EvidenceClaim, FieldDecision, ProfileField, SourceRecord, utcnow

VOLATILE_FIELDS = {ProfileField.organisation, ProfileField.job_title}
EDUCATION_FIELDS = {ProfileField.university_name, ProfileField.degree_type, ProfileField.subject}


def recency_signal(claim: EvidenceClaim, source: SourceRecord, policy: ScoringPolicy, today: date) -> float:
    if claim.field not in VOLATILE_FIELDS:
        return 1
    if claim.is_current is False or claim.end_date is not None:
        return policy.historical_current_factor
    observed = claim.as_of_date or source.published_at
    if observed is None:
        return policy.unknown_recency
    age = max(0, (today - observed).days)
    return 2 ** (-age / policy.current_half_life_days)


def claim_strength(
    claim: EvidenceClaim, source: SourceRecord, policy: ScoringPolicy, today: date | None = None
) -> tuple[float, dict]:
    recency = recency_signal(claim, source, policy, today or utcnow().date())
    identity = min(source.identity.score, claim.identity_relevance)
    directness = policy.directness.get(claim.directness, policy.directness["ambiguous"])
    base = (
        policy.authority_weight * source.authority_score
        + policy.directness_weight * directness
        + policy.recency_weight * recency
    )
    preferred = policy.preferred_bonus if source.preferred_source else 0
    strength = identity * claim.normalisation_certainty * min(1, base + preferred)
    return strength, {
        "authority": source.authority_score,
        "identity": identity,
        "directness": directness,
        "recency": recency,
        "normalisation": claim.normalisation_certainty,
        "preferred_bonus": preferred,
        "base": base,
        "claim_strength": strength,
    }


def confidence_for_group(
    support: list[EvidenceClaim],
    conflicts: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
    today: date | None = None,
) -> tuple[float, dict, list[str], EvidenceClaim]:
    scored = [(claim_strength(c, sources[c.source_id], policy, today), c) for c in support]
    (best, components), selected = max(
        scored, key=lambda x: (x[0][0], x[1].as_of_date or date.min, x[1].claim_id)
    )
    # Same content copied onto another domain is one piece of evidence.
    domain_hashes: dict[str, set[str]] = {}
    for _, claim in scored:
        source = sources[claim.source_id]
        domain_hashes.setdefault(source.domain, set()).add(source.content_hash or source.source_id)
    # Maximum distinct domain/content pairing avoids order-dependent duplicate counting:
    # adding a mirror cannot steal a hash from an existing independent source.
    paired: dict[str, str] = {}

    def assign(domain, visited):
        for content_hash in sorted(domain_hashes[domain]):
            if content_hash in visited:
                continue
            visited.add(content_hash)
            if content_hash not in paired or assign(paired[content_hash], visited):
                paired[content_hash] = domain
                return True
        return False

    independent_count = sum(assign(domain, set()) for domain in sorted(domain_hashes))
    bonus = min(policy.corroboration_cap, policy.corroboration_step * max(0, independent_count - 1))
    strongest_conflict = max(
        (claim_strength(c, sources[c.source_id], policy, today)[0] for c in conflicts), default=0
    )
    penalty = policy.conflict_penalty * strongest_conflict
    # Corroboration cannot defeat weak identity; all parts scale with the best identity.
    corroboration_identity = max(parts["identity"] for (_, parts), _ in scored)
    score = 100 * max(0, min(1, best + bonus * corroboration_identity - penalty))
    reasons = []
    if independent_count < 2:
        reasons.append("LOW_SOURCE_COUNT")
    if conflicts:
        reasons.append("SOURCE_CONFLICT")
    if (
        components["identity"] < policy.identity_review_threshold
        or sources[selected.source_id].identity.ambiguous
    ):
        reasons.append("IDENTITY_AMBIGUITY")
    if selected.directness != "explicit":
        reasons.append("LOW_DIRECTNESS")
    if selected.field in VOLATILE_FIELDS and components["recency"] < policy.unknown_recency:
        reasons.append("OUTDATED_CURRENT_ROLE")
    if selected.field in VOLATILE_FIELDS and not (
        selected.as_of_date or sources[selected.source_id].published_at
    ):
        reasons.append("UNKNOWN_RECENCY")
    if score < policy.review_threshold:
        reasons.append("LOW_CONFIDENCE")
    components = dict(
        components,
        independent_domains=independent_count,
        corroboration_bonus=bonus,
        corroboration_identity=corroboration_identity,
        conflict_strength=strongest_conflict,
        conflict_penalty=penalty,
        formula_version="evidence-v1",
    )
    return round(score, 2), components, reasons, selected


def profile_scores(fields: dict[ProfileField, FieldDecision], policy: ScoringPolicy) -> tuple[float, float]:
    present = {f: d for f, d in fields.items() if d.value is not None}
    coverage = 100 * len(present) / len(ProfileField)
    denominator = sum(policy.field_weights[f] for f in present)
    confidence = (
        sum(policy.field_weights[f] * d.confidence for f, d in present.items()) / denominator
        if denominator
        else 0
    )
    return round(confidence, 2), round(coverage, 2)
