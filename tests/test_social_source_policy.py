"""Hard source exclusions apply before retrieval and at final evidence boundaries."""

import asyncio
import csv
import io
import socket

import pytest

from app.config import ScoringPolicy
from app.export.formats import export_results
from app.research.claims import validate_claims
from app.research.confidence import claim_strength, confidence_for_group
from app.research.discovery import deduplicate_candidates, rank_candidates, source_authority
from app.research.extraction import extract_chunks
from app.research.reconciliation import reconcile
from app.research.source_policy import sanitize_research_result
from app.retrieval.urls import URLValidationError, source_policy_allows, validate_public_url
from app.schemas import (
    EvidenceClaim,
    ExtractedClaim,
    ExtractionResponse,
    FieldDecision,
    IdentityMatch,
    JobResults,
    PersonResultView,
    PersonSeed,
    ProfileField,
    ResearchResult,
    SourceCandidate,
    SourceRecord,
    SourceType,
)

BLOCKED_HOSTS = [
    "linkedin.com",
    "uk.linkedin.com",
    "lnkd.in",
    "static.licdn.com",
    "linkedin.cn",
    "linkedin.com.cn",
    "facebook.com",
    "m.facebook.com",
    "fb.me",
    "instagram.com",
    "x.com",
    "twitter.com",
    "t.co",
    "tiktok.com",
    "vm.tiktok.com",
    "threads.net",
    "threads.com",
    "snapchat.com",
    "pinterest.com",
    "reddit.com",
    "bsky.app",
]
SEED = PersonSeed(full_name="Jane Doe")
POLICY = ScoringPolicy()


def source(url="https://employer.example/jane", **updates):
    data = dict(
        source_id=url,
        person_id="person",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain=url.split("/")[2],
        source_type=SourceType.employer,
        authority_score=1,
        identity=IdentityMatch(score=1, ambiguous=False),
        content_hash=url,
    )
    data.update(updates)
    return SourceRecord(**data)


def claim(item, field=ProfileField.organisation, value="Example Corporation"):
    return EvidenceClaim(
        person_id="person",
        source_id=item.source_id,
        field=field,
        raw_value=value,
        normalised_value=value,
        evidence_text=f"Jane Doe works at {value}.",
        subject_name="Jane Doe",
        identity_relevance=1,
        extraction_model="fixture",
        is_current=True,
    )


@pytest.mark.parametrize("host", BLOCKED_HOSTS)
def test_social_candidates_never_enter_deduplication_or_ranking(host):
    candidate = SourceCandidate(url=f"https://{host}/jane", source_type=SourceType.first_party, score=1)
    assert not source_policy_allows(candidate.url)
    assert deduplicate_candidates([candidate]) == []
    assert rank_candidates([candidate], SEED) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", BLOCKED_HOSTS)
async def test_social_url_is_blocked_before_dns_or_retrieval(monkeypatch, host):
    def no_dns(*args, **kwargs):
        raise AssertionError("Policy exclusion must happen before any network access")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(URLValidationError, match="automated-source policy"):
        await validate_public_url(f"https://{host}/jane")


def test_social_classification_blocks_unlisted_platform_without_domain_false_positives():
    assert (
        deduplicate_candidates(
            [SourceCandidate(url="https://federated.example/jane", source_type=SourceType.social)]
        )
        == []
    )
    assert source_policy_allows("https://linkedin.com.example.org/jane")
    assert source_policy_allows("https://example.org/linkedin.com")


@pytest.mark.parametrize("source_type", [SourceType.government, SourceType.employer, SourceType.university])
def test_official_sources_and_wikipedia_remain_allowed(source_type):
    wiki = SourceCandidate(url="https://en.wikipedia.org/wiki/Jane_Doe", source_type=SourceType.encyclopedia)
    official = SourceCandidate(url="https://official.example/jane", source_type=source_type)
    assert len(deduplicate_candidates([wiki, official])) == 2
    assert rank_candidates([wiki, official], SEED)[0].url == official.url
    assert source_authority(wiki.source_type, wiki.url, POLICY)[0] == 0.60


@pytest.mark.asyncio
async def test_blocked_source_extraction_makes_no_model_call():
    class NoModel:
        def can_parallel_complete(self, *args):
            raise AssertionError("Blocked extraction must not reach a model")

    result = [
        item
        async for item in extract_chunks(
            NoModel(), SEED, ["Jane Doe"], "https://lnkd.in/jane", {}, asyncio.Semaphore(1), 1
        )
    ]
    assert result == []


def test_blocked_source_cannot_validate_claims_or_be_an_observed_profile_link():
    text = "Jane Doe works at Example Corporation."
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=ProfileField.organisation,
                value="Example Corporation",
                evidence=text,
                subject_name="Jane Doe",
            )
        ]
    )
    assert validate_claims(response, SEED, source("https://facebook.com/jane"), text, "fixture") == (
        [],
        ["SOURCE_POLICY_BLOCKED"],
    )
    text = "Jane Doe: https://linkedin.com/in/jane"
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=ProfileField.profile_link,
                value="https://linkedin.com/in/jane",
                evidence=text,
                subject_name="Jane Doe",
            )
        ]
    )
    assert validate_claims(response, SEED, source(), text, "fixture") == ([], ["SOURCE_POLICY_BLOCKED"])


@pytest.mark.parametrize(
    "updates",
    [
        {"requested_url": "https://lnkd.in/redirect"},
        {"final_url": "https://facebook.com/jane"},
        {"canonical_url": "https://instagram.com/jane"},
        {"source_type": SourceType.social},
    ],
)
def test_forbidden_source_cannot_populate_fields_confidence_or_profile_link(updates):
    blocked = source(**updates)
    evidence = claim(blocked)
    profile = reconcile("person", SEED, [evidence], [blocked], POLICY)
    assert profile.fields[ProfileField.organisation].value is None
    assert profile.fields[ProfileField.profile_link].value is None
    assert profile.profile_confidence == 0
    assert profile.sources_used == 0
    assert claim_strength(evidence, blocked, POLICY)[0] == 0


def test_blocked_corroboration_and_conflicts_do_not_change_confidence():
    official, blocked = source(), source("https://linkedin.com/in/jane")
    good, bad = claim(official), claim(blocked)
    sources = {item.source_id: item for item in [official, blocked]}
    baseline = confidence_for_group([good], [], sources, POLICY)
    assert confidence_for_group([good, bad], [bad], sources, POLICY) == baseline


def test_legacy_result_is_reconciled_without_social_evidence_or_model_calls():
    # Simulate a historical accepted source whose URL/classification later proves social.
    official = source()
    evidence = claim(official)
    profile = reconcile("person", SEED, [evidence], [official], POLICY)
    social = official.model_copy(update={"final_url": "https://facebook.com/jane"})
    legacy = ResearchResult(profile=profile, sources=[social], claims=[evidence])
    sanitized = sanitize_research_result(legacy, SEED, POLICY)
    assert sanitized.sources == sanitized.claims == []
    assert sanitized.profile.fields[ProfileField.organisation].value is None
    assert sanitized.profile.fields[ProfileField.profile_link].value is None
    assert sanitized.profile.profile_confidence == 0
    assert sanitized.profile.started_at == profile.started_at
    assert legacy.profile.fields[ProfileField.organisation].value == "Example Corporation"
    clean = ResearchResult(profile=profile, sources=[official], claims=[evidence])
    assert sanitize_research_result(clean, SEED, POLICY) is clean


def test_export_removes_legacy_social_profile_link_and_all_social_provenance():
    official = source()
    social = source("https://facebook.com/jane")
    profile = reconcile("person", SEED, [claim(official)], [official], POLICY)
    for fields in [profile.fields, *(record.fields for record in profile.records)]:
        fields[ProfileField.profile_link] = FieldDecision(
            value=social.final_url, confidence=99, sources=[social.final_url]
        )
        fields[ProfileField.organisation].sources.append("https://lnkd.in/jane")
    result = ResearchResult(profile=profile, sources=[official, social])
    job = JobResults(
        job_id="job",
        status="completed",
        people=[
            PersonResultView(
                person_id="person", row_index=0, original_row={}, status="completed", result=result
            )
        ],
    )
    row = next(
        csv.DictReader(io.StringIO(export_results(job, [], "csv", provenance="field").decode("utf-8-sig")))
    )
    assert row["profile_link"] == ""
    assert row["profile_link_confidence"] == "0"
    assert row["profile_link_source_urls"] == ""
    assert row["organisation_source_urls"] == official.final_url
    assert row["source_urls"] == official.final_url
