from __future__ import annotations

from app.config import ScoringPolicy, Settings
from app.research.discovery import (
    build_fallback_queries,
    build_queries,
    build_query,
    candidate_score,
    deduplicate_candidates,
    query_key,
    rank_candidates,
    source_authority,
)
from app.schemas import PersonSeed, SourceCandidate, SourceType


def settings(**overrides) -> Settings:
    values = {"APP_ENV": "test"}
    values.update(overrides)
    return Settings(**values)


def test_search_query_dedup_ignores_case_whitespace_and_reordered_terms():
    assert query_key('"Jane Doe" education') == query_key('Education  "jane DOE"')
    queries = build_queries(
        PersonSeed(full_name="Jane Doe"),
        settings(),
        extra_queries=['"Jane Doe" education', 'Education "jane DOE"'],
    )
    assert len(queries) == 2


def test_query_dedup_preserves_exclusions_domains_phrases_and_boolean_order():
    for included, excluded in [
        ("Jane Doe physics", "Jane Doe -physics"),
        ("Jane Doe site:example.edu", "Jane Doe -site:example.edu"),
        ("Jane Doe site:example.edu", "Jane Doe site:exampleedu"),
        ('"Jane Doe" research', '"Doe Jane" research'),
        ('Jane Doe -"Example University"', 'Jane Doe "Example University"'),
        ("Jane OR Doe physics", "Jane physics OR Doe"),
    ]:
        assert query_key(included) != query_key(excluded)


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


def test_deduplicate_candidates_blocks_linkedin_host_trees_without_dns(monkeypatch) -> None:
    def no_dns(*args):
        raise AssertionError("Candidate policy checks must not resolve domains")

    monkeypatch.setattr("socket.getaddrinfo", no_dns)
    candidates = [
        *(
            SourceCandidate(url=f"https://{host}/person")
            for host in (
                "linkedin.com",
                "profiles.linkedin.com",
                "lnkd.in",
                "go.lnkd.in",
                "licdn.com",
                "static.licdn.com",
                "linkedin.cn",
                "www.linkedin.cn",
            )
        ),
        SourceCandidate(url="https://notlinkedin.com/person"),
        SourceCandidate(url="https://linkedin.com.attacker.example/person"),
        SourceCandidate(url="https://example.org/linkedin.com/person"),
    ]

    assert [candidate.url for candidate in deduplicate_candidates(candidates, settings())] == [
        "https://notlinkedin.com/person",
        "https://linkedin.com.attacker.example/person",
        "https://example.org/linkedin.com/person",
    ]


def test_government_authority_is_generic_and_not_tied_to_country_domains() -> None:
    policy = ScoringPolicy()

    authority, components = source_authority(
        SourceType.government,
        "https://official-public-body.example/people/jane",
        policy,
    )

    assert authority == policy.authority[SourceType.government]
    assert components["source_type"] == SourceType.government
    assert components["domain_override"] is None


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


def test_fallback_queries_relax_seed_clues_deterministically():
    person = seed()
    queries = build_fallback_queries(person)

    assert queries[0] == '"Jane Doe" Example Org'
    assert '"Jane Doe" Example University' in queries
    assert queries[-1] == '"Jane Doe" biography education career'
    assert build_query(person) not in queries
