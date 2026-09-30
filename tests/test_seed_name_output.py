import csv
import io

from app.config import ScoringPolicy
from app.export.formats import export_results
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import (
    EvidenceClaim,
    IdentityMatch,
    JobResults,
    PersonResultView,
    PersonSeed,
    PersonStatus,
    ProfileField,
    ResearchResult,
    SourceRecord,
    SourceType,
)

PERSON_ID = "person-1"


def _source(
    identifier: str,
    *,
    domain: str | None = None,
    identity: IdentityMatch | None = None,
) -> SourceRecord:
    domain = domain or f"{identifier}.example"
    url = f"https://{domain}/profile"
    return SourceRecord(
        source_id=identifier,
        person_id=PERSON_ID,
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain=domain,
        source_type=SourceType.publication,
        authority_score=0.8,
        identity=identity or IdentityMatch(score=1, ambiguous=False),
    )


def _claim(
    identifier: str,
    source_id: str,
    field: ProfileField,
    value: str,
    *,
    subject_name: str = "Donald Trump",
    fact_group: str | None = None,
) -> EvidenceClaim:
    normalised_value, normalisation_certainty = normalise(field, value)
    return EvidenceClaim(
        claim_id=identifier,
        person_id=PERSON_ID,
        source_id=source_id,
        field=field,
        raw_value=value,
        normalised_value=normalised_value,
        normalisation_certainty=normalisation_certainty,
        evidence_text=f"{subject_name}: {value}",
        subject_name=subject_name,
        identity_relevance=1,
        extraction_model="fixture/extraction",
        fact_group=fact_group,
    )


def test_seed_name_identity_is_immutable_and_display_is_normalized_without_web_evidence():
    profile = reconcile(
        PERSON_ID,
        PersonSeed(full_name="DONALD TRUMP"),
        [],
        [],
        ScoringPolicy(),
    )

    name = profile.fields[ProfileField.full_name]
    assert name.value == "Donald Trump"
    assert profile.input_name == "DONALD TRUMP"
    assert name.confidence == 100
    assert not name.review_required
    assert name.review_reason_codes == []
    assert name.selected_claim_id is None
    assert name.supporting_claim_ids == []
    assert name.supporting_source_ids == []
    assert profile.profile_confidence == 0
    assert profile.coverage == 0


def test_alternate_web_name_cannot_overwrite_or_review_seed_name():
    evidence_source = _source("publication")
    alternate = _claim(
        "alternate-name",
        evidence_source.source_id,
        ProfileField.full_name,
        "Donald J. Trump",
    )

    profile = reconcile(
        PERSON_ID,
        PersonSeed(full_name="Donald Trump"),
        [alternate],
        [evidence_source],
        ScoringPolicy(),
    )

    name = profile.fields[ProfileField.full_name]
    assert name.value == "Donald Trump"
    assert name.confidence == 100
    assert not name.review_required
    assert name.review_reason_codes == []
    assert name.selected_claim_id is None
    assert name.supporting_claim_ids == []
    assert name.supporting_source_ids == []
    assert profile.profile_confidence == 0
    assert profile.coverage == 0


def test_wrong_person_evidence_cannot_populate_enrichment_while_seed_name_remains():
    rejected_identity = IdentityMatch(
        score=1,
        ambiguous=False,
        rejected=True,
        reason_codes=["SEED_IDENTITY_CONTRADICTION"],
    )
    wrong_source = _source("namesake", identity=rejected_identity)
    claims = [
        _claim(
            "wrong-organisation",
            wrong_source.source_id,
            ProfileField.organisation,
            "Namesake Corporation",
        ),
        _claim(
            "wrong-title",
            wrong_source.source_id,
            ProfileField.job_title,
            "Chief Executive Officer",
        ),
    ]

    profile = reconcile(
        PERSON_ID,
        PersonSeed(full_name="Donald Trump", organisation="Trump Organization"),
        claims,
        [wrong_source],
        ScoringPolicy(),
    )

    name = profile.fields[ProfileField.full_name]
    assert name.value == "Donald Trump"
    assert name.confidence == 100
    assert not name.review_required
    assert all(
        decision.value is None
        for field, decision in profile.fields.items()
        if field != ProfileField.full_name
    )
    assert profile.profile_confidence == 0
    assert profile.coverage == 0


def test_every_education_record_repeats_seed_name_and_exports_input_certainty():
    sources = [
        _source("bachelor", domain="bachelor.edu"),
        _source("master", domain="master.edu"),
        _source("doctorate", domain="doctorate.edu"),
    ]
    claims = [
        _claim(
            "bachelor-degree",
            "bachelor",
            ProfileField.degree_type,
            "BSc",
            fact_group="education-bachelor",
        ),
        _claim(
            "master-degree",
            "master",
            ProfileField.degree_type,
            "MSc",
            fact_group="education-master",
        ),
        _claim(
            "doctorate-degree",
            "doctorate",
            ProfileField.degree_type,
            "PhD",
            fact_group="education-doctorate",
        ),
    ]
    profile = reconcile(
        PERSON_ID,
        PersonSeed(full_name="DONALD TRUMP"),
        claims,
        sources,
        ScoringPolicy(),
    )

    assert len(profile.records) == 3
    assert {record.fields[ProfileField.degree_type].value for record in profile.records} == {
        "Bachelor's Degree",
        "Master's Degree",
        "Doctoral Degree",
    }
    for record in profile.records:
        name = record.fields[ProfileField.full_name]
        assert name.value == "Donald Trump"
        assert name.confidence == 100
        assert not name.review_required

    results = JobResults(
        job_id="job-1",
        status="completed",
        people=[
            PersonResultView(
                person_id=PERSON_ID,
                row_index=2,
                original_row={"ALUMNI_FULL_NAME": "DONALD TRUMP"},
                status=PersonStatus.review_required,
                result=ResearchResult(profile=profile, sources=sources, claims=claims),
            )
        ],
    )
    exported = export_results(results, ["ALUMNI_FULL_NAME"], "csv")
    rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))

    assert len(rows) == 3
    assert {row["full_name"] for row in rows} == {"Donald Trump"}
    assert {row["ALUMNI_FULL_NAME"] for row in rows} == {"DONALD TRUMP"}
    assert {float(row["full_name_confidence"]) for row in rows} == {100.0}
