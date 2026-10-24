"""Candidate discovery scoring and deduplication helpers."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import ScoringPolicy, Settings, get_settings
from app.retrieval.urls import canonicalise_url, domain_key, is_blocked_source_host, source_policy_allows
from app.schemas import PersonSeed, SourceCandidate, SourceType
from app.seed_context import seed_context_values


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
    # An encyclopedia cannot become a first-party biography through model labels
    # or domain overrides. Match hostname boundaries, including language/mobile sites.
    encyclopedia = domain == "wikipedia.org" or domain.endswith(".wikipedia.org")
    if encyclopedia:
        effective_type = SourceType.encyclopedia
    score = float(policy.authority.get(effective_type, policy.authority[SourceType.unknown]))
    return score, {
        "source_type": effective_type.value,
        "declared_source_type": source_type.value,
        "domain": domain,
        "domain_override": override.value if override else None,
        "deterministic_classification": "encyclopedia" if encyclopedia else None,
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
        if not source_policy_allows(candidate.url, source_type=candidate.source_type):
            continue
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
        if not source_policy_allows(candidate.url, source_type=candidate.source_type):
            continue
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
    """Start with one useful relationship rather than every supplied attribute."""

    queries = build_context_queries(seed)
    return queries[0] if queries else f'"{seed.full_name}"'


def build_context_queries(
    seed: PersonSeed,
    unresolved_fields: Iterable[str] | None = None,
    *,
    evidence_context: dict[str, list[str]] | None = None,
) -> list[str]:
    """Diversify within the caller's existing budget, revisiting missing objectives.

    Evidence context must come from identity-eligible claims. It can provide a newer
    anchor, while the original seed remains available as a historical hypothesis.
    Each query uses at most two context values; there is no combination expansion.
    """

    fields = set(unresolved_fields) if unresolved_fields is not None else None
    supplied = seed_context_values(seed)
    observed = seed_context_values(evidence_context or {})
    context = {
        key: list(dict.fromkeys([*observed.get(key, []), *supplied.get(key, [])]))
        for key in dict.fromkeys([*observed, *supplied])
    }
    queries: list[str] = []

    def values(*keys: str) -> list[str]:
        return list(dict.fromkeys(value for key in keys for value in context.get(key, [])))

    def add(anchors: list[str], detail: list[str] = (), suffix: str = "") -> None:
        if not anchors:
            return
        # One best anchor per objective keeps wide seed rows from using every slot.
        terms = [f'"{seed.full_name}"', anchors[0]]
        if detail and detail[0].casefold() != anchors[0].casefold():
            terms.append(detail[0])
        if suffix:
            terms.append(suffix)
        queries.append(" ".join(terms)[:600])

    professional = fields is None or bool(fields & {"organisation", "job_title"})
    education = fields is None or bool(fields & {"university_name", "degree_type", "subject"})
    if professional:
        employers = values("organisation", "current_employer", "government_office")
        titles = values("job_title")
        add(employers or titles, titles if employers else [])
    if education:
        universities = values("university_name")
        degrees = values("degree_type") if fields is None or "degree_type" in fields else []
        subjects = values("subject") if fields is None or "subject" in fields else []
        add(
            universities or degrees or subjects,
            (degrees or subjects) if universities else [],
            "" if universities else "education",
        )
    if professional:
        add(values("historical_employer"), values("historical_job_title"), "career")
        add(values("organisation_affiliation", "alumni_organisation"), suffix="biography")
        if not values("organisation", "job_title"):
            add(values("industry"), suffix="career")
    if fields is None and not queries:
        add(values("country", "location", "program_year"), suffix="biography")
    if fields is not None and "country_of_origin" in fields:
        add(values("country_of_origin", "country", "location"), suffix="origin biography")
    return _unique(queries)


def _seed_context_values(seed: PersonSeed) -> list[str]:
    return list(dict.fromkeys(value for values in seed_context_values(seed).values() for value in values))


def build_fallback_queries(seed: PersonSeed) -> list[str]:
    """Relax an over-constrained seed query without widening the configured budget."""

    name = f'"{seed.full_name}"'
    clues = _seed_context_values(seed)
    queries = [*build_context_queries(seed), *(f"{name} {value}"[:600] for value in clues if value)]
    queries.append(f"{name} biography education career")
    base_key = query_key(build_query(seed))
    return [query for query in _unique(queries) if query_key(query) != base_key]


def build_role_institution_query(seed: PersonSeed, job_title: str) -> str:
    """Build one bounded completion query for a strongly evidenced unpaired role."""

    terms = [f'"{seed.full_name}"', f'"{job_title}"']
    context = seed_context_values(seed)
    terms.extend(value for key in ("country", "location") for value in context.get(key, [])[:1])
    terms.append("official")
    return " ".join(terms)[:600]


def build_wikipedia_fallback_query(seed: PersonSeed, unresolved_fields: Iterable[str] = ()) -> str:
    """Build an explicit secondary-source query only for unresolved enrichment."""

    unresolved = set(unresolved_fields)
    terms = [f'"{seed.full_name}"', "site:wikipedia.org"]
    if unresolved & {"organisation", "job_title"}:
        terms.append("career")
    if unresolved & {"university_name", "degree_type", "subject"}:
        terms.append("education")
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
    context = seed_context_values(seed)
    checks = [(seed.full_name, weights["full_name"])]
    checks.extend(
        (value, weights[key])
        for key in ("organisation", "university_name", "job_title", "subject", "country", "location")
        for value in context.get(key, [])[:1]
    )
    score = 0.0
    matched_values: set[str] = set()
    for value, weight in checks:
        key = " ".join(str(value).casefold().split()) if value else ""
        if key and key not in matched_values and key in haystack:
            score += weight
            matched_values.add(key)
    known_attributes = _seed_context_values(seed)
    if known_attributes:
        matched = 0
        for value in known_attributes:
            key = " ".join(value.casefold().split())
            if key and key not in matched_values and key in haystack:
                matched += 1
                matched_values.add(key)
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
