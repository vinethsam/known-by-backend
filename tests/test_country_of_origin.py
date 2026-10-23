"""Offline regressions for evidence-backed Country of Origin decisions."""

import pytest

from app.config import ScoringPolicy
from app.research.claims import validate_claims
from app.research.normalisation import normalise, normalise_country_name
from app.research.reconciliation import reconcile
from app.schemas import (
    EvidenceClaim,
    ExtractedClaim,
    ExtractionResponse,
    IdentityMatch,
    PersonProfile,
    PersonSeed,
    ProfileField,
    SourceRecord,
    SourceType,
)


def _source(identifier: str = "official") -> SourceRecord:
    url = f"https://{identifier}.example.org/jane-doe"
    return SourceRecord(
        source_id=identifier,
        person_id="person",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain=f"{identifier}.example.org",
        source_type=SourceType.first_party,
        authority_score=0.95,
        identity=IdentityMatch(score=1, ambiguous=False),
        content_hash=identifier,
    )


def _extract_origin(
    value: str,
    evidence: str,
    *,
    source: SourceRecord | None = None,
    seed: PersonSeed | None = None,
) -> tuple[list[EvidenceClaim], list[str]]:
    return validate_claims(
        ExtractionResponse(
            claims=[
                ExtractedClaim(
                    field=ProfileField.country_of_origin,
                    value=value,
                    evidence=evidence,
                    subject_name="Jane Doe",
                )
            ]
        ),
        seed or PersonSeed(full_name="Jane Doe"),
        source or _source(),
        evidence,
        "offline-fixture",
    )


def _education_claim(
    identifier: str,
    field: ProfileField,
    value: str,
    group: str,
) -> EvidenceClaim:
    normalised, certainty = normalise(field, value)
    return EvidenceClaim(
        claim_id=identifier,
        person_id="person",
        source_id="official",
        field=field,
        raw_value=value,
        normalised_value=normalised,
        normalisation_certainty=certainty,
        evidence_text=f"Jane Doe earned {value}.",
        subject_name="Jane Doe",
        identity_relevance=1,
        extraction_model="offline-fixture",
        fact_group=group,
    )


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("Kenya", "Kenya"),
        ("united states of america", "United States"),
        ("Sri Lanka", "Sri Lanka"),
    ],
)
def test_country_normalisation_uses_full_iso_country_names(value: str, canonical: str):
    assert normalise_country_name(value) == canonical
    assert normalise(ProfileField.country_of_origin, value) == (canonical, 1)


@pytest.mark.parametrize("value", ["US", "Kenyan", "Nairobi", "somewhere unknown"])
def test_country_normalisation_does_not_guess_codes_nationalities_or_places(value: str):
    assert normalise_country_name(value) is None
    assert normalise(ProfileField.country_of_origin, value) == ("", 0)


@pytest.mark.parametrize(
    "evidence",
    [
        "Jane Doe's country of origin is Kenya.",
        "Jane Doe is from Kenya.",
        "Jane Doe originates from Kenya.",
        "Jane Doe hails from Kenya.",
        "Jane Doe was born and raised in Kenya.",
        "Kenya is Jane Doe's country of origin.",
    ],
)
def test_explicit_origin_statement_is_grounded(evidence: str):
    accepted, reasons = _extract_origin("Kenya", evidence)

    assert reasons == []
    assert len(accepted) == 1
    assert accepted[0].normalised_value == "Kenya"
    assert accepted[0].source_id == "official"
    assert accepted[0].evidence_text == evidence


@pytest.mark.parametrize(
    "evidence",
    [
        "Jane Doe lives in Kenya.",
        "Jane Doe is a citizen of Kenya.",
        "Jane Doe was born in Kenya.",
        "Jane Doe studied at the University of Kenya.",
        "Jane Doe works for the Kenya Ministry of Finance.",
        "Jane Doe is Kenyan.",
        "Jane Doe is not from Kenya.",
        "Kenya is not Jane Doe's country of origin.",
    ],
)
def test_residence_citizenship_birthplace_and_context_are_not_origin(evidence: str):
    accepted, reasons = _extract_origin("Kenya", evidence)

    assert accepted == []
    assert reasons == ["ORIGIN_CONTEXT_UNSUPPORTED"]


def test_unrecognised_country_is_rejected_even_with_explicit_origin_statement():
    accepted, reasons = _extract_origin("US", "Jane Doe is from US.")

    assert accepted == []
    assert reasons == ["UNRECOGNIZED_COUNTRY"]


@pytest.mark.parametrize(
    "evidence",
    [
        "Mary Smith is from Kenya. Jane Doe is a researcher.",
        "Mary Smith is from Kenya, while Jane Doe is a researcher.",
        "Jane Doe profiled Mary Smith, who is from Kenya.",
        "She is from Kenya.",
    ],
)
def test_other_person_or_unnamed_origin_quote_cannot_fill_seeded_person(evidence: str):
    accepted, reasons = _extract_origin("Kenya", evidence)

    assert accepted == []
    assert reasons == ["ORIGIN_CONTEXT_UNSUPPORTED"]


def test_seed_country_is_only_an_identity_hypothesis():
    seed = PersonSeed(full_name="Jane Doe", country="Kenya")
    profile = reconcile("person", seed, [], [], ScoringPolicy())

    origin = profile.fields[ProfileField.country_of_origin]
    assert origin.value is None
    assert origin.confidence == 0
    assert origin.supporting_claim_ids == []
    assert origin.sources == []
    assert profile.legacy_coverage == 0
    assert profile.coverage == 0


def test_reconciliation_rechecks_origin_context_for_prebuilt_claims():
    source = _source()
    grounded, reasons = _extract_origin("Kenya", "Jane Doe is from Kenya.", source=source)
    assert reasons == []
    unsupported = grounded[0].model_copy(update={"evidence_text": "Jane Doe lives in Kenya."})

    profile = reconcile("person", PersonSeed(full_name="Jane Doe"), [unsupported], [source], ScoringPolicy())

    assert profile.fields[ProfileField.country_of_origin].value is None
    assert profile.fields[ProfileField.country_of_origin].supporting_claim_ids == []


def test_reconciliation_rejects_inconsistent_prebuilt_country_normalisation():
    source = _source()
    grounded, reasons = _extract_origin("Kenya", "Jane Doe is from Kenya.", source=source)
    assert reasons == []
    inconsistent = grounded[0].model_copy(update={"normalised_value": "Ghana"})

    profile = reconcile("person", PersonSeed(full_name="Jane Doe"), [inconsistent], [source], ScoringPolicy())

    assert profile.fields[ProfileField.country_of_origin].value is None


def test_verified_origin_has_external_claim_and_source_provenance():
    source = _source()
    claims, reasons = _extract_origin("Kenya", "Jane Doe is from Kenya.", source=source)
    assert reasons == []

    profile = reconcile("person", PersonSeed(full_name="Jane Doe"), claims, [source], ScoringPolicy())
    decision = profile.fields[ProfileField.country_of_origin]

    assert decision.value == "Kenya"
    assert decision.confidence > 0
    assert decision.selected_claim_id == claims[0].claim_id
    assert decision.supporting_claim_ids == [claims[0].claim_id]
    assert decision.supporting_source_ids == [source.source_id]
    assert decision.sources == [source.final_url]
    assert profile.coverage > profile.legacy_coverage


def test_equivalent_full_country_names_do_not_create_a_false_conflict():
    first, second = _source("first"), _source("second")
    short, _ = _extract_origin("United States", "Jane Doe is from United States.", source=first)
    formal, _ = _extract_origin(
        "United States of America", "Jane Doe is from United States of America.", source=second
    )

    profile = reconcile(
        "person",
        PersonSeed(full_name="Jane Doe"),
        [*short, *formal],
        [first, second],
        ScoringPolicy(),
    )

    decision = profile.fields[ProfileField.country_of_origin]
    assert decision.value == "United States"
    assert set(decision.supporting_source_ids) == {first.source_id, second.source_id}
    assert "CONFLICTING_ORIGIN_EVIDENCE" not in decision.review_reason_codes


def test_conflicting_reliable_origin_evidence_requires_review():
    first, second = _source("first"), _source("second")
    kenya, kenya_reasons = _extract_origin("Kenya", "Jane Doe is from Kenya.", source=first)
    ghana, ghana_reasons = _extract_origin("Ghana", "Jane Doe is from Ghana.", source=second)
    assert kenya_reasons == ghana_reasons == []

    profile = reconcile(
        "person",
        PersonSeed(full_name="Jane Doe"),
        [*kenya, *ghana],
        [first, second],
        ScoringPolicy(),
    )
    decision = profile.fields[ProfileField.country_of_origin]

    assert decision.value is None
    assert decision.review_required
    assert set(decision.conflicting_claim_ids) == {kenya[0].claim_id, ghana[0].claim_id}
    assert "CONFLICTING_ORIGIN_EVIDENCE" in decision.review_reason_codes
    assert "CONFLICTING_ORIGIN_EVIDENCE" in profile.records[0].review_reason_codes


def test_person_origin_is_shared_across_separate_credential_records():
    source = _source()
    origin, reasons = _extract_origin("Kenya", "Jane Doe is from Kenya.", source=source)
    assert reasons == []
    claims = [
        *origin,
        _education_claim("first-university", ProfileField.university_name, "Example University", "first"),
        _education_claim("first-degree", ProfileField.degree_type, "MSc", "first"),
        _education_claim("second-university", ProfileField.university_name, "Other University", "second"),
        _education_claim("second-degree", ProfileField.degree_type, "MBA", "second"),
    ]

    profile = reconcile("person", PersonSeed(full_name="Jane Doe"), claims, [source], ScoringPolicy())

    assert len(profile.records) == 2
    for record in profile.records:
        decision = record.fields[ProfileField.country_of_origin]
        assert decision.value == "Kenya"
        assert decision.supporting_claim_ids == [origin[0].claim_id]
        assert decision.supporting_source_ids == [source.source_id]


def test_old_stored_profile_without_origin_or_legacy_coverage_is_readable():
    profile = reconcile("person", PersonSeed(full_name="Jane Doe"), [], [], ScoringPolicy())
    stored = profile.model_dump(mode="json")
    stored["fields"].pop("country_of_origin")
    stored.pop("legacy_coverage")
    stored["fields"]["organisation"].update(value="Example Foundation", confidence=80, review_required=False)
    stored["profile_confidence"] = 80
    stored["coverage"] = 16.67  # One of six researched fields in the old stored schema.
    for record in stored["records"]:
        record["fields"].pop("country_of_origin")
        record.pop("legacy_coverage")
        record["fields"]["organisation"].update(
            value="Example Foundation", confidence=80, review_required=False
        )
        record["profile_confidence"] = 80
        record["coverage"] = 16.67

    restored = PersonProfile.model_validate(stored)

    assert restored.fields[ProfileField.country_of_origin].value is None
    assert restored.coverage == 14.29
    assert restored.legacy_coverage == 16.67
    assert restored.records[0].fields[ProfileField.country_of_origin].value is None
    assert restored.records[0].coverage == 14.29
    assert restored.records[0].legacy_coverage == 16.67


@pytest.mark.asyncio
async def test_origin_from_existing_pages_adds_no_provider_or_search_calls(tmp_path, monkeypatch):
    # Reuse the production orchestrator and its offline provider fixtures. Only
    # the page text changes between runs; every provider response is otherwise
    # the same, including its origin extraction candidate.
    from test_pipeline_api import configured, fake_services, migrated_store

    base_text = (
        "Jane Doe works for Example Foundation as Programme Director. "
        "Jane Doe earned BSc in Economics at Example University."
    )
    mapping = {
        "full_name": "Jane Doe",
        "organisation": "Example Foundation",
        "job_title": "Programme Director",
        "university_name": "Example University",
        "degree_type": "BSc",
        "subject": "Economics",
        "country_of_origin": "Kenya",
    }
    runs = []
    for label, text in (
        ("without-origin", base_text),
        ("with-origin", base_text + " Jane Doe is from Kenya."),
    ):
        directory = tmp_path / label
        directory.mkdir()
        settings = configured(directory)
        store = migrated_store(settings)
        pipeline, calls = fake_services(
            settings,
            store,
            monkeypatch,
            page_text=text,
            extraction_mapping=mapping,
        )
        result = await pipeline.research(
            f"job-{label}", "person", PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
        )
        runs.append((result, calls))

    without, baseline_calls = runs[0]
    with_origin, origin_calls = runs[1]
    assert without.profile.fields[ProfileField.country_of_origin].value is None
    assert with_origin.profile.fields[ProfileField.country_of_origin].value == "Kenya"
    assert origin_calls["models"] == baseline_calls["models"]
    assert len(origin_calls["queries"]) == len(baseline_calls["queries"])
    assert with_origin.profile.metrics.llm_calls == without.profile.metrics.llm_calls
