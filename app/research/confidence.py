"""Deterministic evidence scoring; scores are heuristics, not calibrated probabilities."""

from __future__ import annotations

from datetime import date

from app.config import ScoringPolicy
from app.schemas import EvidenceClaim, FieldDecision, ProfileField, SourceRecord, SourceType, utcnow

VOLATILE_FIELDS = {ProfileField.organisation, ProfileField.job_title}
EDUCATION_FIELDS = {ProfileField.university_name, ProfileField.degree_type, ProfileField.subject}


def claim_tie_key(claim: EvidenceClaim, sources: dict[str, SourceRecord]) -> tuple[str, ...]:
    """Stable evidence ordering that never depends on generated UUIDs."""

    source = sources[claim.source_id]
    return (
        source.canonical_url or source.final_url or source.requested_url,
        claim.field.value,
        claim.normalised_value,
        claim.raw_value,
        claim.as_of_date.isoformat() if claim.as_of_date else "",
        claim.end_date.isoformat() if claim.end_date else "",
        "" if claim.is_current is None else str(claim.is_current),
        claim.directness,
        claim.fact_group or "",
        claim.evidence_location or "",
        claim.evidence_text,
    )


def _independent_pairs(claims: list[EvidenceClaim], sources: dict[str, SourceRecord]) -> dict[str, str]:
    """Return a maximum distinct content-hash -> domain pairing."""

    domain_hashes: dict[str, set[str]] = {}
    for claim in claims:
        source = sources[claim.source_id]
        content_hash = source.content_hash or source.source_id
        domain_hashes.setdefault(source.domain, set()).add(content_hash)
    paired: dict[str, str] = {}

    def assign(domain: str, visited: set[str]) -> bool:
        for content_hash in sorted(domain_hashes[domain]):
            if content_hash in visited:
                continue
            visited.add(content_hash)
            if content_hash not in paired or assign(paired[content_hash], visited):
                paired[content_hash] = domain
                return True
        return False

    for domain in sorted(domain_hashes):
        assign(domain, set())
    return paired


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
    claim: EvidenceClaim,
    source: SourceRecord,
    policy: ScoringPolicy,
    today: date | None = None,
    *,
    effective_identities: dict[str, float] | None = None,
    field_quality: float = 1,
) -> tuple[float, dict]:
    recency = recency_signal(claim, source, policy, today or utcnow().date())
    baseline_identity = min(source.identity.score, claim.identity_relevance)
    identity = max(baseline_identity, (effective_identities or {}).get(source.source_id, 0))
    directness = policy.directness.get(claim.directness, policy.directness["ambiguous"])
    base = (
        policy.authority_weight * source.authority_score
        + policy.directness_weight * directness
        + policy.recency_weight * recency
    )
    preferred = policy.preferred_bonus if source.preferred_source else 0
    quality = max(0, min(1, field_quality))
    strength = identity * claim.normalisation_certainty * quality * min(1, base + preferred)
    return strength, {
        "authority": source.authority_score,
        "baseline_identity": baseline_identity,
        "identity": identity,
        "directness": directness,
        "recency": recency,
        "normalisation": claim.normalisation_certainty,
        "field_quality": quality,
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
    *,
    effective_identities: dict[str, float] | None = None,
    quality_by_claim: dict[str, float] | None = None,
) -> tuple[float, dict, list[str], EvidenceClaim]:
    quality_by_claim = quality_by_claim or {}
    field_identities = dict(effective_identities or {})
    name_identity_floor = 0.0
    if support and support[0].field == ProfileField.full_name:
        authoritative_exact = [
            claim
            for claim in support
            if claim.directness == "explicit"
            and claim.normalisation_certainty >= 0.9
            and sources[claim.source_id].authority_score >= policy.authority[SourceType.publication]
        ]
        if len(_independent_pairs(authoritative_exact, sources)) >= 2:
            # This uplift applies only to the exact-name field. It does not promote
            # either source into a trusted identity anchor for unrelated facts.
            name_identity_floor = min(1.0, policy.identity_review_threshold + policy.corroboration_step / 2)
            for claim in authoritative_exact:
                field_identities[claim.source_id] = max(
                    field_identities.get(claim.source_id, 0), name_identity_floor
                )
    scored = [
        (
            claim_strength(
                claim,
                sources[claim.source_id],
                policy,
                today,
                effective_identities=field_identities,
                field_quality=quality_by_claim.get(claim.claim_id, 1),
            ),
            claim,
        )
        for claim in support
    ]
    (best, components), selected = min(
        scored,
        key=lambda item: (
            -item[0][0],
            -(item[1].as_of_date or date.min).toordinal(),
            claim_tie_key(item[1], sources),
        ),
    )
    # Same content copied onto another domain is one piece of evidence.
    pair_scores: dict[tuple[str, str], tuple[float, EvidenceClaim]] = {}
    for (strength, _), claim in scored:
        source = sources[claim.source_id]
        content_hash = source.content_hash or source.source_id
        pair = (source.domain, content_hash)
        candidate = (strength, claim)
        if (
            pair not in pair_scores
            or candidate[0] > pair_scores[pair][0]
            or (
                candidate[0] == pair_scores[pair][0]
                and claim_tie_key(candidate[1], sources) < claim_tie_key(pair_scores[pair][1], sources)
            )
        ):
            pair_scores[pair] = candidate
    # Maximum distinct domain/content pairing avoids order-dependent duplicate counting:
    # adding a mirror cannot steal a hash from an existing independent source.
    paired = _independent_pairs(support, sources)
    independent_count = len(paired)
    independent_strengths = sorted(
        (pair_scores[(domain, content_hash)][0] for content_hash, domain in paired.items()), reverse=True
    )
    # Each independent source contributes in proportion to its own evidence quality.
    # A pile of weak directories therefore cannot receive the same lift as first-party support.
    bonus = min(
        policy.corroboration_cap,
        sum(policy.corroboration_step * strength for strength in independent_strengths[1:]),
    )
    strongest_conflict = max(
        (
            claim_strength(
                claim,
                sources[claim.source_id],
                policy,
                today,
                effective_identities=field_identities,
                field_quality=quality_by_claim.get(claim.claim_id, 1),
            )[0]
            for claim in conflicts
        ),
        default=0,
    )
    penalty = policy.conflict_penalty * strongest_conflict
    corroboration_identity = max(parts["identity"] for (_, parts), _ in scored)
    score = 100 * max(0, min(1, best + bonus - penalty))
    reasons = []
    if independent_count < 2:
        reasons.append("LOW_SOURCE_COUNT")
    if conflicts:
        reasons.append("SOURCE_CONFLICT")
    if (
        components["identity"] < policy.identity_review_threshold
        or sources[selected.source_id].identity.signals.get("model_relevance") == "ambiguous"
    ):
        reasons.append("IDENTITY_AMBIGUITY")
    if selected.directness != "explicit":
        reasons.append("LOW_DIRECTNESS")
    if components["field_quality"] < 0.8:
        reasons.append("LOW_FIELD_QUALITY")
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
        formula_version="evidence-v2",
        name_identity_floor=name_identity_floor,
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
