"""Candidate discovery scoring and deduplication helpers."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import ScoringPolicy, Settings, get_settings
from app.retrieval.urls import canonicalise_url, domain_key, is_blocked_source_host
from app.schemas import PersonSeed, SourceCandidate, SourceType


@dataclass(frozen=True)
class UrlParts:
    url: str
    domain: str


def source_authority(source_type: SourceType, url: str, policy: ScoringPolicy) -> tuple[float, dict]:
    domain = _domain_for_url(url)
    effective_type = source_type
    override = _matching_override(domain, policy)
    if override is not None:
        effective_type = override
    score = float(policy.authority.get(effective_type, policy.authority[SourceType.unknown]))
    return score, {
        "source_type": effective_type.value,
        "declared_source_type": source_type.value,
        "domain": domain,
        "domain_override": override.value if override else None,
        "base_score": score,
    }


def candidate_score(candidate: SourceCandidate, seed: PersonSeed, settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    policy = settings.SCORING
    authority, _ = source_authority(candidate.source_type, candidate.url, settings.SCORING)
    relevance = policy.candidate_relevance_weights.get(
        candidate.relevance,
        policy.candidate_relevance_weights["ambiguous"],
    )
    identity = _identity_signal(candidate, seed, policy.candidate_identity_weights)
    preferred = 1.0 if candidate.preferred_source else 0.0
    score = (
        policy.candidate_authority_weight * authority
        + policy.candidate_relevance_weight * relevance
        + policy.candidate_identity_weight * identity
        + policy.candidate_preferred_weight * preferred
    )
    return max(0.0, min(1.0, round(score, 6)))


def deduplicate_candidates(
    candidates: Iterable[SourceCandidate],
    settings: Settings | None = None,
) -> list[SourceCandidate]:
    by_canonical: dict[str, SourceCandidate] = {}
    for candidate in candidates:
        parts = _valid_url_parts(candidate.url)
        if parts is None:
            continue
        canonical = _canonical_url(parts.url)
        current = candidate.model_copy(update={"url": parts.url, "domain": candidate.domain or parts.domain})
        existing = by_canonical.get(canonical)
        if existing is None or current.score > existing.score:
            by_canonical[canonical] = current
    return list(by_canonical.values())


def rank_candidates(
    candidates: Iterable[SourceCandidate],
    seed: PersonSeed | None = None,
    settings: Settings | None = None,
    *,
    limit: int | None = None,
) -> list[SourceCandidate]:
    settings = settings or get_settings()
    scored: list[SourceCandidate] = []
    for candidate in candidates:
        score = candidate.score
        if seed is not None:
            score = candidate_score(candidate, seed, settings)
        scored.append(candidate.model_copy(update={"score": score}))

    buckets: dict[str, list[SourceCandidate]] = defaultdict(list)
    for candidate in scored:
        buckets[_domain_key(candidate.url)].append(candidate)
    queues = [deque(sorted(bucket, key=lambda item: (-item.score, item.url))) for bucket in buckets.values()]
    queues.sort(key=lambda queue: (-queue[0].score, queue[0].domain, queue[0].url))

    ranked: list[SourceCandidate] = []
    while queues and (limit is None or len(ranked) < limit):
        next_round: list[deque[SourceCandidate]] = []
        for queue in queues:
            if limit is not None and len(ranked) >= limit:
                break
            ranked.append(queue.popleft())
            if queue:
                next_round.append(queue)
        queues = sorted(next_round, key=lambda queue: (-queue[0].score, queue[0].domain, queue[0].url))
    return ranked


def build_query(seed: PersonSeed) -> str:
    terms = [f'"{seed.full_name}"']
    for value in (
        seed.organisation,
        seed.university_name,
        seed.job_title,
        seed.subject,
        seed.country,
        seed.location,
    ):
        if value:
            terms.append(str(value))
    return " ".join(terms)[:600]


def build_queries(
    seed: PersonSeed, settings: Settings | None = None, *, extra_queries: Iterable[str] = ()
) -> list[str]:
    settings = settings or get_settings()
    budget = settings.MAX_SEARCH_QUERIES_PER_PERSON
    base_queries = _unique([build_query(seed), *(" ".join(query.split())[:600] for query in extra_queries)])
    return base_queries[:budget]


def _identity_signal(candidate: SourceCandidate, seed: PersonSeed, weights: dict[str, float]) -> float:
    haystack = f"{candidate.title} {candidate.snippet} {candidate.url}".casefold()
    checks = [
        (seed.full_name, weights["full_name"]),
        (seed.organisation, weights["organisation"]),
        (seed.university_name, weights["university_name"]),
        (seed.job_title, weights["job_title"]),
        (seed.subject, weights["subject"]),
        (seed.country, weights["country"]),
        (seed.location, weights["location"]),
    ]
    score = 0.0
    for value, weight in checks:
        if value and str(value).casefold() in haystack:
            score += weight
    if seed.known_attributes:
        matched = sum(1 for value in seed.known_attributes.values() if value.casefold() in haystack)
        score += min(weights["known_attributes_cap"], matched * weights["known_attribute"])
    return max(0.0, min(1.0, score))


def _matching_override(domain: str, policy: ScoringPolicy) -> SourceType | None:
    for raw_domain, source_type in sorted(
        policy.domain_overrides.items(), key=lambda item: len(item[0]), reverse=True
    ):
        override_domain = _domain_from_override(raw_domain)
        if domain == override_domain or domain.endswith("." + override_domain):
            return source_type
    return None


def _domain_from_override(value: str) -> str:
    parsed = urlsplit(value if "://" in value else "https://" + value)
    host = parsed.hostname or value
    return host.lower().removeprefix("www.").rstrip(".")


def _valid_url_parts(url: str) -> UrlParts | None:
    try:
        canonical = canonicalise_url(url)
    except ValueError:
        return None
    parsed = urlsplit(canonical)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower().removeprefix("www.").rstrip(".")
    if is_blocked_source_host(host):
        return None
    if "." not in host and host != "localhost":
        return None
    return UrlParts(url=canonical, domain=host)


def _canonical_url(url: str) -> str:
    return canonicalise_url(url)


def _domain_key(url: str) -> str:
    return domain_key(url)


def _domain_for_url(url: str) -> str:
    parsed = urlsplit(canonicalise_url(url))
    return (parsed.hostname or "").lower().removeprefix("www.").rstrip(".")


def query_key(query: str) -> str:
    """Deduplicate reordered terms without erasing exclusions or quoted phrases."""
    normalized = " ".join(query.casefold().split())
    terms = re.findall(r'(?:[^\s"]|"[^"]*")+', normalized)
    # Boolean grouping is order-sensitive; preserve its expression as supplied.
    if any(term in {"or", "and", "not"} for term in terms) or any(c in normalized for c in "()"):
        return normalized
    return " ".join(sorted(set(terms)))


def _unique(values: Iterable[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split())
        key = query_key(normalized)
        if normalized and key not in seen:
            unique.append(normalized)
            seen.add(key)
    return unique
