"""Offline identity patterns; names label fixtures, not captured live biographies."""

import pytest
from test_pipeline_api import configured, fake_services, migrated_store

from app.config import ScoringPolicy
from app.research.claims import validate_claims
from app.research.identity import (
    assess_identity,
    candidate_has_seed_identity_context,
    eligible_identity_source_ids,
)
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import (
    EvidenceClaim,
    ExtractedClaim,
    ExtractionResponse,
    PersonSeed,
    ProfileField,
    SourceCandidate,
    SourceRecord,
    SourceType,
)
from app.worker import process_lease

POLICY = ScoringPolicy()


def _source(key: str, seed: PersonSeed, text: str, *, relevance: str = "likely") -> SourceRecord:
    url = f"https://{key}.example.org/biography"
    return SourceRecord(
        source_id=key,
        person_id="person",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain=f"{key}.example.org",
        title=f"{seed.full_name} biography",
        source_type=SourceType.employer,
        authority_score=POLICY.authority[SourceType.employer],
        content_hash=key,
        identity=assess_identity(seed, text, POLICY, model_relevance=relevance),
    )


def _claim(source: SourceRecord, seed: PersonSeed, field: ProfileField, value: str, evidence: str):
    normalized, certainty = normalise(field, value)
    return EvidenceClaim(
        person_id="person",
        source_id=source.source_id,
        field=field,
        raw_value=value,
        normalised_value=normalized,
        normalisation_certainty=certainty,
        evidence_text=evidence,
        subject_name=seed.full_name,
        identity_relevance=source.identity.score,
        extraction_model="offline-fixture",
        fact_group="employment",
        is_current=True,
    )


def test_official_seed_anchored_biography_survives_advisor_false_negative():
    seed = PersonSeed(full_name="Alexander Karp", organisation="Example Analytics")
    text = "Alexander Karp is Chief Executive Officer of Example Analytics."
    match = assess_identity(seed, text, POLICY, model_relevance="unrelated", source_type=SourceType.employer)

    assert not match.rejected
    assert match.signals["matched_seed_anchors"] == ["organisation"]
    assert match.ambiguous  # Preserve the advisor disagreement for review.
    assert match.signals["advisor_relevance"] == "unrelated"


@pytest.mark.asyncio
async def test_disputed_candidate_is_retrieved_then_verified_against_its_body(tmp_path, monkeypatch):
    settings = configured(
        tmp_path,
        MAX_SEARCH_QUERIES_PER_PERSON=1,
        MAX_SOURCE_MODEL_TOOL_CALLS=1,
        MAX_SOURCES_PER_PERSON=1,
        SOURCES_PER_ROUND=1,
    )
    store = migrated_store(settings)
    url = "https://employer.example.org/jane"
    pipeline, calls = fake_services(
        settings,
        store,
        monkeypatch,
        search_urls=[url],
        advisor_relevance="unrelated",
        page_text="Jane Doe is Programme Director at Example Foundation.",
        extraction_mapping={"organisation": "Example Foundation", "job_title": "Programme Director"},
        current_claim_fields={"organisation", "job_title"},
    )
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    job = store.create_job([seed])
    await process_lease(store, pipeline, store.claim_task("worker"), settings)

    result = store.get_results(job.job_id).people[0].result
    assert calls["fetched"] == [url]
    assert result.profile.fields[ProfileField.organisation].value == "Example Foundation"
    assert result.profile.fields[ProfileField.job_title].value == "Programme Director"
    assert result.sources[0].identity.signals["advisor_relevance"] == "unrelated"
    assert result.profile.review_required


def test_candidate_metadata_only_opens_retrieval_when_name_and_seed_institution_agree():
    seed = PersonSeed(full_name="Alexander Karp", organisation="Example Analytics")
    candidate = SourceCandidate(
        url="https://official.example.org/alexander-karp",
        title="Alexander Karp | Example Analytics",
    )
    split_metadata = candidate.model_copy(
        update={"title": "Alexander Karp", "snippet": "Chief Executive Officer at Example Analytics"}
    )
    unrelated = candidate.model_copy(update={"title": "Alexander Karp | Other Company", "snippet": ""})
    partial = candidate.model_copy(update={"title": "Alex Karp | Example Analytics", "snippet": ""})

    assert candidate_has_seed_identity_context(seed, candidate)
    assert candidate_has_seed_identity_context(seed, split_metadata)
    assert not candidate_has_seed_identity_context(seed, unrelated)
    assert not candidate_has_seed_identity_context(seed, partial)


def test_disputed_candidate_metadata_cannot_validate_a_different_fetched_person():
    seed = PersonSeed(full_name="Alexander Karp", organisation="Example Analytics")
    candidate = SourceCandidate(
        url="https://directory.example.org/biography",
        title="Alexander Karp | Example Analytics",
    )

    assert candidate_has_seed_identity_context(seed, candidate)
    fetched = assess_identity(
        seed,
        "Alex Karp leads Other Company.",
        POLICY,
        model_relevance="unrelated",
        source_type=SourceType.employer,
    )
    assert fetched.rejected


def test_unanchored_claims_about_partial_namesake_do_not_gain_identity_from_footer():
    seed = PersonSeed(full_name="Bassel Mohamed Ali", organisation="Seed Organisation")
    page = "Bassel Ali leads Other Company. This is his current role. Related search: Bassel Mohamed Ali."
    sources = [_source(key, seed, page) for key in ("first", "second")]
    claims = [
        _claim(source, seed, ProfileField.organisation, "Other Company", "Bassel Ali leads Other Company.")
        for source in sources
    ]
    eligible = eligible_identity_source_ids(
        seed, claims, {source.source_id: source for source in sources}, POLICY
    )
    profile = reconcile("person", seed, claims, sources, POLICY)

    assert profile.fields[ProfileField.organisation].value is None
    assert eligible == set()
    assert len(claims) == 2  # Evidence remains available for audit.


def test_disputed_footer_anchor_cannot_admit_partial_namesake_claims():
    seed = PersonSeed(full_name="Bassel Mohamed Ali", organisation="Seed Organisation")
    page = "Bassel Ali leads Other Company. Related search: Bassel Mohamed Ali at Seed Organisation."
    sources = [_source(key, seed, page, relevance="unrelated") for key in ("first", "second")]
    for source in sources:
        source.identity = assess_identity(
            seed,
            page,
            POLICY,
            model_relevance="unrelated",
            source_type=SourceType.employer,
        )
    claims = [
        _claim(source, seed, ProfileField.organisation, "Other Company", "Bassel Ali leads Other Company.")
        for source in sources
    ]

    eligible = eligible_identity_source_ids(
        seed, claims, {source.source_id: source for source in sources}, POLICY
    )
    profile = reconcile("person", seed, claims, sources, POLICY)

    assert eligible == set()
    assert profile.fields[ProfileField.organisation].value is None


def test_disputed_mixed_page_rejects_partial_namesake_claim_even_with_valid_anchor():
    seed = PersonSeed(full_name="Alexander Karp", organisation="Example Analytics")
    anchored = "Alexander Karp is Chief Executive Officer at Example Analytics."
    namesake = "Alex Karp is Chief Executive Officer at Aardvark Company."
    page = f"{anchored} {namesake}"
    source = _source("mixed", seed, page, relevance="unrelated")
    source.identity = assess_identity(
        seed, page, POLICY, model_relevance="unrelated", source_type=SourceType.employer
    )
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=ProfileField.organisation,
                value="Example Analytics",
                evidence=anchored,
                subject_name=seed.full_name,
                fact_group="valid-role",
                is_current=True,
            ),
            ExtractedClaim(
                field=ProfileField.organisation,
                value="Aardvark Company",
                evidence=namesake,
                subject_name=seed.full_name,
                fact_group="namesake-role",
                is_current=True,
            ),
        ]
    )

    validated, reasons = validate_claims(response, seed, source, page, "offline-fixture")
    assert [claim.raw_value for claim in validated] == ["Example Analytics"]
    assert "DISPUTED_CLAIM_IDENTITY_CONTEXT" in reasons

    # Reconciliation also protects older stored claims that bypassed validation.
    old_claims = [
        _claim(source, seed, ProfileField.organisation, "Example Analytics", anchored),
        _claim(source, seed, ProfileField.organisation, "Aardvark Company", namesake),
    ]
    profile = reconcile("person", seed, old_claims, [source], POLICY)
    assert profile.fields[ProfileField.organisation].value == "Example Analytics"


def test_disputed_same_sentence_namesakes_cannot_cross_validate_claims():
    seed = PersonSeed(full_name="Alexander Karp", organisation="Example Analytics")
    page = "Alexander Karp is CEO at Example Analytics, while Alex Karp is CEO at Aardvark Company."
    source = _source("mixed-clause", seed, page, relevance="unrelated")
    source.identity = assess_identity(
        seed, page, POLICY, model_relevance="unrelated", source_type=SourceType.employer
    )
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=ProfileField.organisation,
                value=value,
                evidence=page,
                subject_name=seed.full_name,
                is_current=True,
            )
            for value in ("Example Analytics", "Aardvark Company")
        ]
    )

    validated, reasons = validate_claims(response, seed, source, page, "offline-fixture")
    assert validated == []
    assert "DISPUTED_CLAIM_IDENTITY_CONTEXT" in reasons
    old_claims = [
        _claim(source, seed, ProfileField.organisation, value, page)
        for value in ("Example Analytics", "Aardvark Company")
    ]
    profile = reconcile("person", seed, old_claims, [source], POLICY)
    assert profile.fields[ProfileField.organisation].value is None


def test_current_corrective_cluster_with_full_seed_name_remains_eligible():
    seed = PersonSeed(full_name="Hellen Munyasa", organisation="Old Organisation")
    text = "Hellen Munyasa is now Director at New Organisation."
    sources = [_source(key, seed, text) for key in ("first", "second")]
    claims = [_claim(source, seed, ProfileField.organisation, "New Organisation", text) for source in sources]

    assert eligible_identity_source_ids(
        seed, claims, {source.source_id: source for source in sources}, POLICY
    ) == {"first", "second"}


def test_full_name_biography_heading_can_ground_a_following_pronoun_role():
    seed = PersonSeed(full_name="Hellen Munyasa", organisation="Old Organisation")
    page = "Hellen Munyasa. She is Director at New Organisation."
    quote = "She is Director at New Organisation."
    sources = [_source(key, seed, page) for key in ("first", "second")]
    claims = [
        _claim(source, seed, ProfileField.organisation, "New Organisation", quote) for source in sources
    ]

    assert eligible_identity_source_ids(
        seed, claims, {source.source_id: source for source in sources}, POLICY
    ) == {"first", "second"}
