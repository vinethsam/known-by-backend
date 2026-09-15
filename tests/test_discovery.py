from __future__ import annotations

from app.config import ScoringPolicy, Settings
from app.research.discovery import (
    build_queries,
    build_query,
    candidate_score,
    deduplicate_candidates,
    rank_candidates,
    source_authority,
)
from app.schemas import PersonSeed, SourceCandidate, SourceType


def settings(**overrides) -> Settings:
    values = {"APP_ENV": "test", "SEARCH_MIN_INTERVAL_SECONDS": 0}
    values.update(overrides)
    return Settings(**values)


def seed() -> PersonSeed:
    return PersonSeed(full_name="Jane Doe", organisation="Example Org", university_name="Example University")


def test_source_authority_uses_domain_override_with_suffix_boundary() -> None:
    policy = ScoringPolicy(domain_overrides={"example.edu": SourceType.university})

    overridden, components = source_authority(SourceType.unknown, "https://people.example.edu/jane", policy)
    not_overridden, other_components = source_authority(
        SourceType.unknown, "https://badexample.edu/jane", policy
    )

    assert overridden == policy.authority[SourceType.university]
    assert components["domain_override"] == "university"
    assert not_overridden == policy.authority[SourceType.unknown]
    assert other_components["domain_override"] is None


def test_candidate_score_keeps_authority_relevance_and_identity_distinct() -> None:
    candidate = SourceCandidate(
        url="https://example.edu/jane",
        title="Jane Doe - Example University",
        snippet="Jane Doe works with Example Org.",
        source_type=SourceType.university,
        relevance="likely",
    )
    unrelated = candidate.model_copy(
        update={"relevance": "unrelated", "title": "Other Person", "snippet": ""}
    )

    assert candidate_score(candidate, seed(), settings()) > candidate_score(unrelated, seed(), settings())


def test_deduplicate_candidates_rejects_invalid_urls_across_arbitrary_domains() -> None:
    app_settings = settings()
    candidates = [
        SourceCandidate(url="https://employer.example.org/jane", title="Jane", score=0.3),
        SourceCandidate(url="https://example.edu/profiles/jane?utm_source=x", title="Jane low", score=0.2),
        SourceCandidate(url="https://example.edu/profiles/jane", title="Jane high", score=0.9),
        SourceCandidate(url="https://outside.example.net/jane", title="Outside", score=1.0),
        SourceCandidate(url="not-a-url", title="Invalid", score=1.0),
        SourceCandidate(url="https://user@example.edu/profiles/secret", title="Credentialed", score=1.0),
    ]

    deduped = deduplicate_candidates(candidates, app_settings)

    assert [candidate.url for candidate in deduped] == [
        "https://employer.example.org/jane",
        "https://example.edu/profiles/jane",
        "https://outside.example.net/jane",
    ]
    assert deduped[1].title == "Jane high"
    assert deduped[1].domain == "example.edu"


def test_rank_candidates_diversifies_domains_before_domain_duplicates() -> None:
    candidates = [
        SourceCandidate(url="https://a.example/1", domain="a.example", score=0.99),
        SourceCandidate(url="https://a.example/2", domain="a.example", score=0.98),
        SourceCandidate(url="https://b.example/1", domain="b.example", score=0.7),
    ]

    ranked = rank_candidates(candidates)

    assert [candidate.url for candidate in ranked] == [
        "https://a.example/1",
        "https://b.example/1",
        "https://a.example/2",
    ]


def test_build_queries_respects_budget_without_artificial_site_constraints() -> None:
    app_settings = settings(MAX_SEARCH_QUERIES_PER_PERSON=2)

    queries = build_queries(seed(), app_settings, extra_queries=["extra query"])

    assert len(queries) == 2
    assert all("site:" not in query for query in queries)
    assert queries[1] == "extra query"


def test_build_query_uses_seed_clues() -> None:
    query = build_query(seed())

    assert '"Jane Doe"' in query
    assert "Example Org" in query
    assert "Example University" in query
