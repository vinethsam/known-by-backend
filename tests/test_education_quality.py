"""Offline quality fixtures: retain supported partial education without inventing degrees."""

from datetime import date

import pytest

from app.config import ScoringPolicy
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, IdentityMatch, PersonSeed, ProfileField, SourceRecord, SourceType


def source(identifier="official", *, authority=0.95, identity=1, rejected=False):
    return SourceRecord(
        source_id=identifier,
        person_id="person",
        requested_url=f"https://{identifier}.example/biography",
        final_url=f"https://{identifier}.example/biography",
        canonical_url=f"https://{identifier}.example/biography",
        domain=f"{identifier}.example",
        source_type=SourceType.university,
        authority_score=authority,
        identity=IdentityMatch(score=identity, rejected=rejected, ambiguous=identity < 0.8),
        content_hash=identifier,
    )


def claim(identifier, field, value, *, group=None, quote=None, source_id="official", **kwargs):
    normalised, certainty = normalise(field, value)
    return EvidenceClaim(
        claim_id=identifier,
        person_id="person",
        source_id=source_id,
        field=field,
        raw_value=value,
        normalised_value=normalised,
        normalisation_certainty=certainty,
        subject_name="Isaac Lungu",
        identity_relevance=1,
        extraction_model="offline-fixture",
        fact_group=group,
        evidence_text=quote or f"Isaac Lungu studied {value}.",
        **kwargs,
    )


def profile_for(claims, sources=None):
    return reconcile(
        "person", PersonSeed(full_name="Isaac Lungu"), claims, sources or [source()], ScoringPolicy()
    )


def test_reliable_university_only_evidence_survives_without_inventing_a_degree():
    university = claim("university", ProfileField.university_name, "University of Example")
    profile = profile_for([university])

    assert len(profile.records) == 1
    fields = profile.records[0].fields
    assert fields[ProfileField.university_name].value == "University of Example"
    assert fields[ProfileField.university_name].supporting_claim_ids == ["university"]
    assert fields[ProfileField.degree_type].value is None
    assert fields[ProfileField.subject].value is None


def test_degree_and_subject_survive_when_university_is_unknown():
    profile = profile_for(
        [
            claim("masters", ProfileField.degree_type, "Master's degree", group="masters"),
            claim("economics", ProfileField.subject, "Economics", group="masters"),
        ]
    )

    assert len(profile.records) == 1
    fields = profile.records[0].fields
    assert fields[ProfileField.degree_type].value == "Master's Degree"
    assert fields[ProfileField.subject].value == "Economics"
    assert fields[ProfileField.university_name].value is None


def test_same_source_same_credential_quote_joins_ungrouped_components():
    quote = "Isaac Lungu earned a Master's degree in Economics at University of Example."
    profile = profile_for(
        [
            claim("masters", ProfileField.degree_type, "Master's degree", quote=quote),
            claim("economics", ProfileField.subject, "Economics", quote=quote),
            claim("university", ProfileField.university_name, "University of Example", quote=quote),
        ]
    )

    assert len(profile.records) == 1
    fields = profile.records[0].fields
    assert fields[ProfileField.degree_type].value == "Master's Degree"
    assert fields[ProfileField.subject].value == "Economics"
    assert fields[ProfileField.university_name].value == "University of Example"


def test_shared_source_or_section_does_not_attach_unrelated_partial_university():
    profile = profile_for(
        [
            claim(
                "masters",
                ProfileField.degree_type,
                "Master's degree",
                group="masters",
                evidence_location="Education",
            ),
            claim(
                "university", ProfileField.university_name, "Other University", evidence_location="Education"
            ),
        ]
    )

    degree_record = next(
        record for record in profile.records if record.fields[ProfileField.degree_type].value
    )
    assert degree_record.fields[ProfileField.university_name].value is None


@pytest.mark.parametrize("weakness", ["authority", "identity", "directness"])
def test_weak_university_fragments_remain_internal_alternatives(weakness):
    fixture_source = source(
        authority=0.45 if weakness == "authority" else 0.95,
        identity=0.65 if weakness == "identity" else 1,
    )
    university = claim(
        "fragment",
        ProfileField.university_name,
        "University of Example",
        directness="ambiguous" if weakness == "directness" else "explicit",
    )
    profile = profile_for([university], [fixture_source])

    assert len(profile.records) == 1
    field = profile.records[0].fields[ProfileField.university_name]
    assert field.value is None
    assert "fragment" in field.alternative_claim_ids


def test_subject_only_fragment_does_not_invent_a_credential():
    profile = profile_for([claim("fragment", ProfileField.subject, "Economics")])

    assert len(profile.records) == 1
    assert profile.records[0].record_id == "general"
    assert profile.records[0].fields[ProfileField.subject].value is None
    assert "fragment" in profile.records[0].fields[ProfileField.subject].alternative_claim_ids


def test_university_fragment_fitting_two_degrees_is_not_guessed_onto_either():
    claims = [
        claim("university", ProfileField.university_name, "University of Example"),
    ]
    quote = "Isaac Lungu earned a Master's degree and PhD at University of Example."
    claims[0].evidence_text = quote
    for identifier, value in (("masters", "Master's degree"), ("phd", "PhD")):
        claims.append(claim(identifier, ProfileField.degree_type, value, group=identifier, quote=quote))
    profile = profile_for(claims)

    assert len(profile.records) == 2
    assert all(record.fields[ProfileField.university_name].value is None for record in profile.records)
    assert "university" in profile.fields[ProfileField.university_name].alternative_claim_ids


def test_wrong_person_partial_education_cannot_join_an_accepted_credential():
    profile = profile_for(
        [
            claim("masters", ProfileField.degree_type, "Master's degree", group="masters"),
            claim("wrong-person", ProfileField.university_name, "Other University", source_id="namesake"),
        ],
        [source(), source("namesake", identity=0.2, rejected=True)],
    )

    assert len(profile.records) == 1
    field = profile.records[0].fields[ProfileField.university_name]
    assert field.value is None
    assert "wrong-person" not in field.supporting_claim_ids + field.alternative_claim_ids


def test_isaac_lungu_overlapping_doctoral_subjects_do_not_duplicate_qualifications():
    claims = []
    for group, degree, subject in [
        ("doctorate-a", "PhD", "Business Administration"),
        ("doctorate-b", "Doctor of Philosophy", "Management"),
        ("masters-a", "Master's degree", "Economics"),
        ("masters-b", "Master's degree", "Economic Policy"),
    ]:
        claims.extend(
            [
                claim(
                    f"{group}-university", ProfileField.university_name, "University of Example", group=group
                ),
                claim(f"{group}-degree", ProfileField.degree_type, degree, group=group),
                claim(f"{group}-subject", ProfileField.subject, subject, group=group, directness="implied"),
            ]
        )
    profile = profile_for(claims)

    assert len(profile.records) == 2
    assert sorted(record.fields[ProfileField.degree_type].value for record in profile.records) == [
        "Doctoral Degree",
        "Master's Degree",
    ]
    for record in profile.records:
        assert record.fields[ProfileField.university_name].value == "University of Example"
        assert record.fields[ProfileField.subject].conflicting_claim_ids


def test_positive_distinct_dates_still_allow_two_same_level_credentials():
    claims = []
    for year in (2010, 2015):
        group = str(year)
        claims.extend(
            [
                claim(
                    f"{group}-university", ProfileField.university_name, "University of Example", group=group
                ),
                claim(
                    f"{group}-degree",
                    ProfileField.degree_type,
                    "Master's degree",
                    group=group,
                    end_date=date(year, 6, 1),
                ),
            ]
        )
    profile = profile_for(claims)
    assert len(profile.records) == 2
    assert all(
        record.fields[ProfileField.degree_type].value == "Master's Degree" for record in profile.records
    )
