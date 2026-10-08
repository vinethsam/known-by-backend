"""Partial failures preserve independently valid, grounded, identity-safe evidence."""

from datetime import date

import pytest
from test_golden_quality import POLICY, fact, page, profile
from test_pipeline_api import configured, fake_services, migrated_store

from app.research.claims import validate_claims
from app.schemas import ExtractedClaim, ExtractionResponse, PersonSeed, ProfileField, SourceType
from app.worker import process_lease


@pytest.mark.asyncio
async def test_jennifer_shape_salvages_coherent_evidence_across_partial_source_failures(
    tmp_path, monkeypatch
):
    settings = configured(
        tmp_path,
        MAX_SEARCH_QUERIES_PER_PERSON=1,
        MAX_SOURCE_MODEL_TOOL_CALLS=1,
        MAX_SOURCES_PER_PERSON=6,
        SOURCES_PER_ROUND=6,
        TARGET_FIELD_CONFIDENCE=100,
    )
    store = migrated_store(settings)
    urls = [f"https://source-{index}.example{index}.org/jane" for index in range(6)]
    failed = set(urls[:3])
    text = (
        "Jane Doe is Programme Director at New Foundation. "
        "Jane Doe attended Example University and earned an MSc in Economics."
    )

    def claims_for_source(payload):
        index = urls.index(payload["source_url"])
        if index == 3:
            return {
                "organisation": "New Foundation",
                "job_title": "Programme Director",
                "university_name": "Example University",
            }
        if index == 4:
            return {
                "organisation": "New Foundation",
                "job_title": "Programme Director",
                "degree_type": "MSc",
            }
        return {
            "organisation": "New Foundation",
            "job_title": "Programme Director",
            "university_name": "Example University",
            # Deliberately absent from source text: only this claim is rejected.
            "subject": "Unsupported Subject",
        }

    pipeline, _ = fake_services(
        settings,
        store,
        monkeypatch,
        search_urls=urls,
        failed_fetch_urls=failed,
        page_text=text,
        extraction_mapping=claims_for_source,
        current_claim_fields={"organisation", "job_title"},
    )
    job = store.create_job([PersonSeed(full_name="Jane Doe", organisation="Seed Organisation")])
    await process_lease(store, pipeline, store.claim_task("worker"), settings)

    person = store.get_results(job.job_id).people[0]
    result = person.result
    assert person.status in {"completed", "review_required"}, person.model_dump(mode="json")
    assert person.error_code is None
    assert result.profile.fields[ProfileField.organisation].value == "New Foundation"
    assert result.profile.fields[ProfileField.job_title].value == "Programme Director"
    assert [source.processing_status for source in result.sources].count("failed") == 3
    assert [source.processing_status for source in result.sources].count("extracted") == 3
    assert result.profile.metrics.sources_accepted == 3
    assert result.profile.metrics.claims_extracted == 10
    assert result.profile.metrics.claims_rejected == 1
    assert result.profile.metrics.stop_reason == "MAX_SOURCES"
    assert result.profile.metrics.failure_reason is None
    assert result.profile.research_status in {"needs_review", "partial_coverage"}
    assert "RETRIEVAL_FAILED" in result.profile.metrics.error_codes


def test_unsupported_education_date_drops_only_date_and_preserves_bundle():
    seed = PersonSeed(full_name="Jane Doe")
    source = page("university", seed, "", kind=SourceType.university, secure=True)
    evidence = "Jane Doe earned an MSc in Economics at Example University."
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=field,
                value=value,
                evidence=evidence,
                subject_name=seed.full_name,
                fact_group="education",
                as_of_date=date(2020, 1, 1) if field == ProfileField.degree_type else None,
            )
            for field, value in [
                (ProfileField.university_name, "Example University"),
                (ProfileField.degree_type, "MSc"),
                (ProfileField.subject, "Economics"),
            ]
        ]
    )
    rejected, adjusted = {}, {}
    claims, reasons = validate_claims(
        response,
        seed,
        source,
        evidence,
        "fixture",
        rejection_counts=rejected,
        adjustment_counts=adjusted,
    )
    result = profile(seed, [source], claims)
    assert "UNGROUNDED_CLAIM_DATE" in reasons
    assert rejected == {}
    assert adjusted == {"UNGROUNDED_CLAIM_DATE": 1}
    assert next(claim for claim in claims if claim.field == ProfileField.degree_type).as_of_date is None
    assert result.fields[ProfileField.university_name].value == "Example University"
    assert result.fields[ProfileField.degree_type].value == "Master's Degree"
    assert result.fields[ProfileField.subject].value == "Economics"


def test_invalid_claim_in_source_does_not_remove_valid_employment_claims():
    seed = PersonSeed(full_name="Jane Doe")
    source = page("employer", seed, "", kind=SourceType.employer, secure=True)
    evidence = "Jane Doe is Programme Director at Example Foundation."
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=ProfileField.organisation,
                value="Example Foundation",
                evidence=evidence,
                subject_name=seed.full_name,
                fact_group="employment",
            ),
            ExtractedClaim(
                field=ProfileField.job_title,
                value="Programme Director",
                evidence=evidence,
                subject_name=seed.full_name,
                fact_group="employment",
            ),
            ExtractedClaim(
                field=ProfileField.degree_type,
                value="Imagined Degree",
                evidence=evidence,
                subject_name=seed.full_name,
                fact_group="education",
            ),
        ]
    )
    rejected = {}
    claims, reasons = validate_claims(response, seed, source, evidence, "fixture", rejection_counts=rejected)
    result = profile(seed, [source], claims)
    assert reasons == ["UNGROUNDED_VALUE"]
    assert rejected == {"UNGROUNDED_VALUE": 1}
    assert result.fields[ProfileField.organisation].value == "Example Foundation"
    assert result.fields[ProfileField.job_title].value == "Programme Director"
    assert result.fields[ProfileField.degree_type].value is None


@pytest.mark.asyncio
async def test_all_retrieval_failures_retain_specific_terminal_meaning(tmp_path, monkeypatch):
    settings = configured(
        tmp_path,
        MAX_SEARCH_QUERIES_PER_PERSON=1,
        MAX_SOURCE_MODEL_TOOL_CALLS=1,
        MAX_SOURCES_PER_PERSON=2,
        SOURCES_PER_ROUND=2,
    )
    store = migrated_store(settings)
    urls = ["https://down-one.example/jane", "https://down-two.example/jane"]
    pipeline, _ = fake_services(settings, store, monkeypatch, search_urls=urls, failed_fetch_urls=set(urls))
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    await process_lease(store, pipeline, store.claim_task("worker"), settings)

    person = store.get_results(job.job_id).people[0]
    assert person.status == "failed"
    assert person.error_code == "RETRIEVAL_FAILED"
    assert person.result.profile.research_status == "retryable_research_failure"
    assert person.result.profile.metrics.failure_reason == "RETRIEVAL_FAILED"


@pytest.mark.asyncio
async def test_failed_recovery_does_not_erase_earlier_university(tmp_path, monkeypatch):
    settings = configured(
        tmp_path,
        MAX_SEARCH_QUERIES_PER_PERSON=2,
        MAX_SOURCE_MODEL_TOOL_CALLS=2,
        MAX_SOURCES_PER_PERSON=2,
        SOURCES_PER_ROUND=1,
        TARGET_FIELD_CONFIDENCE=100,
    )
    store = migrated_store(settings)
    initial = "https://university.example.edu/jane"
    recovery = "https://unavailable.example.org/jane"

    def results_for_round(_query, round_number):
        return [initial] if round_number == 1 else [initial, recovery]

    pipeline, _ = fake_services(
        settings,
        store,
        monkeypatch,
        search_urls=results_for_round,
        failed_fetch_urls={recovery},
        page_text="Jane Doe attended Example University.",
        extraction_mapping={"university_name": "Example University"},
    )
    job = store.create_job([PersonSeed(full_name="Jane Doe", university_name="Example University")])
    await process_lease(store, pipeline, store.claim_task("worker"), settings)

    person = store.get_results(job.job_id).people[0]
    assert person.status in {"completed", "review_required"}, person.model_dump(mode="json")
    assert person.result.profile.fields[ProfileField.university_name].value == "Example University"
    assert any(source.processing_status == "failed" for source in person.result.sources)
    assert person.result.profile.metrics.failure_reason is None


@pytest.mark.asyncio
async def test_grounded_but_disconnected_identity_evidence_is_not_retrieval_failure(tmp_path, monkeypatch):
    settings = configured(
        tmp_path,
        MAX_SEARCH_QUERIES_PER_PERSON=1,
        MAX_SOURCE_MODEL_TOOL_CALLS=1,
        MAX_SOURCES_PER_PERSON=2,
        SOURCES_PER_ROUND=2,
    )
    store = migrated_store(settings)
    urls = ["https://alpha.example-one.org/jane", "https://beta.example-two.org/jane"]

    def claims_for_source(payload):
        return (
            {"organisation": "Alpha Company"}
            if payload["source_url"] == urls[0]
            else {"university_name": "Beta University"}
        )

    pipeline, _ = fake_services(
        settings,
        store,
        monkeypatch,
        search_urls=urls,
        page_text="Jane Doe works at Alpha Company and attended Beta University.",
        extraction_mapping=claims_for_source,
    )
    job = store.create_job([PersonSeed(full_name="Jane Doe", organisation="Seed Organisation")])
    await process_lease(store, pipeline, store.claim_task("worker"), settings)

    person = store.get_results(job.job_id).people[0]
    assert person.status == "failed"
    assert person.error_code == "IDENTITY_UNRESOLVED"
    assert person.result.profile.metrics.failure_reason == "IDENTITY_UNRESOLVED"
    assert person.result.profile.research_status == "insufficient_evidence"
    assert all(source.fetch_status == 200 for source in person.result.sources)
    assert person.result.claims
    assert "RETRIEVAL_FAILED" not in person.result.profile.metrics.error_codes


@pytest.mark.asyncio
async def test_retrieved_source_with_only_ungrounded_claims_reports_grounding_failure(tmp_path, monkeypatch):
    settings = configured(
        tmp_path,
        MAX_SEARCH_QUERIES_PER_PERSON=1,
        MAX_SOURCE_MODEL_TOOL_CALLS=1,
        MAX_SOURCES_PER_PERSON=1,
        SOURCES_PER_ROUND=1,
    )
    store = migrated_store(settings)
    pipeline, _ = fake_services(
        settings,
        store,
        monkeypatch,
        search_urls=["https://retrieved.example.org/jane"],
        page_text="Jane Doe has a public biography.",
        extraction_mapping={"organisation": "Fabricated Organisation"},
    )
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    await process_lease(store, pipeline, store.claim_task("worker"), settings)

    person = store.get_results(job.job_id).people[0]
    assert person.status == "failed"
    assert person.error_code == "GROUNDING_FAILED"
    assert person.result.profile.research_status == "insufficient_evidence"
    assert person.result.profile.metrics.claims_extracted == 1
    assert person.result.profile.metrics.claims_rejected == 1
    assert person.result.profile.metrics.failure_reason == "GROUNDING_FAILED"
    assert person.result.sources[0].fetch_status == 200
    assert person.result.sources[0].processing_status == "extracted"
    assert "RETRIEVAL_FAILED" not in person.result.profile.metrics.error_codes


def test_disconnected_seed_mismatch_sources_remain_identity_ineligible():
    seed = PersonSeed(full_name="Jane Doe", organisation="Seed Organisation")
    business = page("business", seed, "", kind=SourceType.employer, secure=False)
    university = page("university", seed, "", kind=SourceType.university, secure=False)
    # Recreate the normal unanchored exact-name identity score without bypassing safety.
    for source in (business, university):
        source.identity.score = POLICY.identity_name_only
        source.identity.rejected = False
        source.identity.signals = {
            "exact_name": True,
            "subject_matches": True,
            "seed_anchor_required": True,
            "matched_seed_anchors": [],
        }
    claims = [
        fact(business, seed, ProfileField.organisation, "Alpha Company", group="employment"),
        fact(university, seed, ProfileField.university_name, "Beta University", group="education"),
    ]
    result = profile(seed, [business, university], claims)
    assert result.coverage == 0
    assert result.fields[ProfileField.organisation].value is None
    assert result.fields[ProfileField.university_name].value is None


def test_failed_seed_anchored_source_without_claims_cannot_block_coherent_survivors():
    seed = PersonSeed(full_name="Jane Doe", organisation="Seed Organisation")
    failed_anchor = page("seed-anchor", seed, "", kind=SourceType.employer, secure=True)
    failed_anchor.processing_status = "extraction_failed"
    failed_anchor.error_code = "EXTRACTION_PROVIDER_FAILED"
    failed_anchor.identity.signals = {
        "seed_anchor_required": True,
        "matched_seed_anchors": ["organisation"],
    }
    first = page(
        "corrective-one",
        seed,
        "Jane Doe is Programme Director at New Foundation.",
        kind=SourceType.publication,
    )
    second = page(
        "corrective-two",
        seed,
        "Jane Doe is Programme Director at New Foundation.",
        kind=SourceType.publication,
    )
    claims = [
        *[
            fact(
                source,
                seed,
                ProfileField.organisation,
                "New Foundation",
                group="employment",
                current=True,
            )
            for source in (first, second)
        ],
        *[
            fact(
                source,
                seed,
                ProfileField.job_title,
                "Programme Director",
                group="employment",
                current=True,
            )
            for source in (first, second)
        ],
    ]
    result = profile(seed, [failed_anchor, first, second], claims)
    assert result.fields[ProfileField.organisation].value == "New Foundation"
    assert result.fields[ProfileField.job_title].value == "Programme Director"
