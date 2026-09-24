"""Authority never substitutes for identifying the person a page describes."""

from __future__ import annotations

from collections import defaultdict

from app.config import ScoringPolicy
from app.research.normalisation import comparison_key, name_key
from app.schemas import EvidenceClaim, IdentityMatch, PersonSeed, ProfileField, SourceRecord, SourceType


def contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


def assess_identity(
    seed: PersonSeed,
    text: str,
    policy: ScoringPolicy,
    subject_name: str | None = None,
    model_relevance: str | None = None,
) -> IdentityMatch:
    key = comparison_key(text)
    target = name_key(seed.full_name)
    exact = contains_phrase(key, target)
    subject_matches = subject_name is None or name_key(subject_name) == target
    signals = {
        "exact_name": exact,
        "subject_matches": subject_matches,
        "matched_clues": [],
        "model_relevance": model_relevance,
    }
    if not exact or not subject_matches:
        return IdentityMatch(rejected=True, signals=signals, reason_codes=["IDENTITY_MISMATCH"])
    clues = {
        k: getattr(seed, k)
        for k in ("organisation", "country", "location", "university_name", "subject", "program_year")
    }
    clues.update(seed.known_attributes)
    matched_values = set()
    for label, value in clues.items():
        if value and name_key(value) != target and contains_phrase(key, comparison_key(value)):
            matched_values.add(comparison_key(value))
            signals["matched_clues"].append(label)
    score = min(1, policy.identity_name_only + len(matched_values) * policy.identity_clue_bonus)
    # LLM judgement may constrain a match; it cannot raise same-name evidence to certainty.
    if model_relevance == "unrelated":
        return IdentityMatch(rejected=True, signals=signals, reason_codes=["IDENTITY_MISMATCH"])
    if model_relevance == "ambiguous":
        score = min(score, policy.identity_review_threshold)
    ambiguous = model_relevance == "ambiguous" or score < policy.identity_review_threshold
    return IdentityMatch(
        score=score,
        ambiguous=ambiguous,
        signals=signals,
        reason_codes=["IDENTITY_AMBIGUITY"] if ambiguous else [],
    )


def effective_identity_scores(
    claims: list[EvidenceClaim],
    sources: dict[str, SourceRecord],
    policy: ScoringPolicy,
) -> dict[str, float]:
    """Return immutable, cross-source identity scores for reconciliation and discovery.

    Only grounded, explicit context repeated by a genuinely independent source can
    strengthen a name-only match. The source's stored identity assessment remains the
    baseline and is never mutated, so calling this function repeatedly is idempotent.
    """

    scores = {source_id: source.identity.score for source_id, source in sources.items()}
    context_fields = {
        ProfileField.organisation,
        ProfileField.job_title,
        ProfileField.university_name,
        ProfileField.degree_type,
        ProfileField.subject,
    }
    minimum_context_authority = policy.authority[SourceType.directory]
    key_sources: dict[tuple[ProfileField, str], set[str]] = defaultdict(set)
    source_keys: dict[str, set[tuple[ProfileField, str]]] = defaultdict(set)

    for claim in claims:
        source = sources.get(claim.source_id)
        if (
            source is None
            or source.identity.rejected
            or source.identity.score < policy.identity_minimum
            or claim.identity_relevance < policy.identity_minimum
            or source.authority_score < minimum_context_authority
            or claim.directness != "explicit"
            or claim.field not in context_fields
            or not claim.normalised_value
        ):
            continue
        key = (claim.field, claim.normalised_value)
        key_sources[key].add(claim.source_id)
        source_keys[claim.source_id].add(key)

    def independent(left_id: str, right_id: str) -> bool:
        left, right = sources[left_id], sources[right_id]
        left_hash = left.content_hash or left.source_id
        right_hash = right.content_hash or right.source_id
        return left.domain != right.domain and left_hash != right_hash

    def independent_peer_count(peer_ids: set[str]) -> int:
        """Count peers with a maximum distinct domain/content pairing."""

        domain_hashes: dict[str, set[str]] = defaultdict(set)
        for peer_id in peer_ids:
            peer = sources[peer_id]
            domain_hashes[peer.domain].add(peer.content_hash or peer.source_id)
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
        return len(paired)

    importance = {
        ProfileField.organisation: 1.0,
        ProfileField.university_name: 1.0,
        ProfileField.job_title: 0.65,
        ProfileField.subject: 0.65,
        # A degree level alone is common and therefore only a weak identity clue.
        ProfileField.degree_type: 0.25,
    }
    for source_id, keys in source_keys.items():
        source = sources[source_id]
        if source.identity.rejected:
            continue
        corroborated: list[tuple[ProfileField, str]] = []
        peers: set[str] = set()
        for key in keys:
            independent_peers = {
                other_id
                for other_id in key_sources[key]
                if other_id != source_id and independent(source_id, other_id)
            }
            if independent_peers:
                corroborated.append(key)
                peers.update(independent_peers)
        if not corroborated:
            continue
        context_bonus = sum(policy.identity_clue_bonus * importance[field] for field, _ in corroborated)
        context_bonus = min(2 * policy.identity_clue_bonus, context_bonus)
        peer_bonus = min(
            policy.corroboration_cap,
            policy.corroboration_step * independent_peer_count(peers),
        )
        strengthened = min(1.0, source.identity.score + context_bonus + peer_bonus)
        # An explicit model ambiguity may constrain a candidate but can never reject it.
        if source.identity.signals.get("model_relevance") == "ambiguous":
            strengthened = min(strengthened, policy.identity_review_threshold)
        scores[source_id] = strengthened
    return scores
