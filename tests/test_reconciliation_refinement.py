"""Focused regressions for conservative education-credential reconciliation."""

from datetime import date

from app.config import ScoringPolicy
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import (
    EvidenceClaim,
    IdentityMatch,
    PersonSeed,
    ProfileField,
    SourceRecord,
    SourceType,
)


def source() -> SourceRecord:
    return SourceRecord(
        source_id="education-source",
        person_id="person",
        requested_url="https://university.example/jane-doe",
        final_url="https://university.example/jane-doe",
        canonical_url="https://university.example/jane-doe",
        domain="university.example",
        source_type=SourceType.university,
        authority_score=0.95,
        identity=IdentityMatch(score=1),
        content_hash="education-source",
    )


def claim(
    identifier: str,
    field: ProfileField,
    value: str,
    fact_group: str,
    *,
    end_date: date | None = None,
) -> EvidenceClaim:
    normalised_value, certainty = normalise(field, value)
    return EvidenceClaim(
        claim_id=identifier,
        person_id="person",
        source_id="education-source",
        field=field,
        raw_value=value,
        normalised_value=normalised_value,
        normalisation_certainty=certainty,
        evidence_text=f"Jane Doe earned {value}.",
        subject_name="Jane Doe",
        identity_relevance=1,
        extraction_model="fixture",
        fact_group=fact_group,
        end_date=end_date,
    )


def profile_for(claims: list[EvidenceClaim]):
    return reconcile(
        "person",
        PersonSeed(full_name="Jane Doe"),
        claims,
        [source()],
        ScoringPolicy(),
    )


def test_location_qualified_institution_variant_does_not_split_one_credential():
    claims = [
        claim("dundee", ProfileField.university_name, "University of Dundee", "degree-one"),
        claim("degree-one", ProfileField.degree_type, "MSc", "degree-one"),
        claim(
            "dundee-scotland",
            ProfileField.university_name,
            "University of Dundee, Scotland",
            "degree-two",
        ),
        claim("degree-two", ProfileField.degree_type, "Master's", "degree-two"),
    ]

    profile = profile_for(claims)

    assert len(profile.records) == 1
    record = profile.records[0]
    assert record.fields[ProfileField.degree_type].value == "Master's Degree"
    assert record.fields[ProfileField.university_name].value in {
        "University of Dundee",
        "University of Dundee, Scotland",
    }


def test_distinct_campuses_with_shared_university_prefix_remain_separate_credentials():
    claims = [
        claim(
            "berkeley",
            ProfileField.university_name,
            "University of California, Berkeley",
            "berkeley-degree",
        ),
        claim("berkeley-degree", ProfileField.degree_type, "MSc", "berkeley-degree"),
        claim(
            "los-angeles",
            ProfileField.university_name,
            "University of California, Los Angeles",
            "los-angeles-degree",
        ),
        claim("los-angeles-degree", ProfileField.degree_type, "Master's", "los-angeles-degree"),
    ]

    profile = profile_for(claims)

    assert len(profile.records) == 2
    assert {record.fields[ProfileField.university_name].value for record in profile.records} == {
        "University of California, Berkeley",
        "University of California, Los Angeles",
    }


def test_generic_university_evidence_is_not_assigned_to_one_of_multiple_campuses():
    claims = [
        claim(
            "berkeley",
            ProfileField.university_name,
            "University of California, Berkeley",
            "a-berkeley-degree",
        ),
        claim("berkeley-degree", ProfileField.degree_type, "MSc", "a-berkeley-degree"),
        claim(
            "los-angeles",
            ProfileField.university_name,
            "University of California, Los Angeles",
            "b-los-angeles-degree",
        ),
        claim("los-angeles-degree", ProfileField.degree_type, "Master's", "b-los-angeles-degree"),
        claim(
            "generic-university",
            ProfileField.university_name,
            "University of California",
            "z-generic-degree",
        ),
        claim("generic-degree", ProfileField.degree_type, "Master's", "z-generic-degree"),
    ]

    profile = profile_for(claims)

    assert len(profile.records) == 2
    assert {record.fields[ProfileField.university_name].value for record in profile.records} == {
        "University of California, Berkeley",
        "University of California, Los Angeles",
    }
    for record in profile.records:
        university = record.fields[ProfileField.university_name]
        degree = record.fields[ProfileField.degree_type]
        assert "generic-university" not in university.supporting_claim_ids
        assert "generic-degree" not in degree.supporting_claim_ids
        assert "generic-university" in university.alternative_claim_ids
        assert "generic-degree" in degree.alternative_claim_ids
        assert "EDUCATION_GROUPING_AMBIGUITY" in record.review_reason_codes


def test_distinct_qualification_labels_at_same_institution_remain_separate_credentials():
    claims = [
        claim("example-msc", ProfileField.university_name, "Example University", "msc"),
        claim("msc", ProfileField.degree_type, "MSc", "msc"),
        claim("example-mba", ProfileField.university_name, "Example University", "mba"),
        claim("mba", ProfileField.degree_type, "MBA", "mba"),
    ]

    profile = profile_for(claims)

    assert len(profile.records) == 2
    assert {record.fields[ProfileField.degree_type].selected_claim_id for record in profile.records} == {
        "msc",
        "mba",
    }
    assert all(
        record.fields[ProfileField.degree_type].value == "Master's Degree" for record in profile.records
    )


def test_materially_distinct_credential_dates_remain_separate_credentials():
    claims = [
        claim("example-2010", ProfileField.university_name, "Example University", "degree-2010"),
        claim(
            "msc-2010",
            ProfileField.degree_type,
            "MSc",
            "degree-2010",
            end_date=date(2010, 6, 1),
        ),
        claim("example-2015", ProfileField.university_name, "Example University", "degree-2015"),
        claim(
            "msc-2015",
            ProfileField.degree_type,
            "MSc",
            "degree-2015",
            end_date=date(2015, 6, 1),
        ),
    ]

    profile = profile_for(claims)

    assert len(profile.records) == 2
    assert {record.fields[ProfileField.degree_type].selected_claim_id for record in profile.records} == {
        "msc-2010",
        "msc-2015",
    }


def test_same_institution_and_level_merge_differing_subjects_as_a_conflict():
    claims = [
        claim("turku-one", ProfileField.university_name, "University of Turku", "degree-one"),
        claim("masters-one", ProfileField.degree_type, "MSc", "degree-one"),
        claim("political", ProfileField.subject, "Political Science", "degree-one"),
        claim("turku-two", ProfileField.university_name, "University of Turku", "degree-two"),
        claim("masters-two", ProfileField.degree_type, "Master's", "degree-two"),
        claim("social", ProfileField.subject, "Social Sciences", "degree-two"),
    ]

    profile = profile_for(claims)

    assert len(profile.records) == 1
    subject = profile.records[0].fields[ProfileField.subject]
    assert subject.value in {"Political Science", "Social Sciences"}
    assert {subject.selected_claim_id, *subject.conflicting_claim_ids} == {"political", "social"}


def test_clearly_distinct_degree_levels_remain_separate_credentials():
    claims: list[EvidenceClaim] = []
    for index, degree in enumerate(("BSc", "MSc", "PhD"), start=1):
        group = f"degree-{index}"
        claims.extend(
            [
                claim(
                    f"university-{index}",
                    ProfileField.university_name,
                    "University of Example",
                    group,
                ),
                claim(f"degree-{index}", ProfileField.degree_type, degree, group),
            ]
        )

    profile = profile_for(claims)

    assert len(profile.records) == 3
    assert {record.fields[ProfileField.degree_type].value for record in profile.records} == {
        "Bachelor's Degree",
        "Master's Degree",
        "Doctoral Degree",
    }


def test_reliable_university_only_evidence_preserves_partial_education():
    profile = profile_for(
        [
            claim(
                "orphan-university",
                ProfileField.university_name,
                "University of Example",
                "education-fragment",
            )
        ]
    )

    assert len(profile.records) == 1
    record = profile.records[0]
    assert record.fields[ProfileField.university_name].value == "University of Example"
    assert record.fields[ProfileField.degree_type].value is None
    assert record.fields[ProfileField.subject].value is None
