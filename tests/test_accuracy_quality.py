"""Accuracy regressions for deterministic normalisation and reconciliation."""

from datetime import date

import pytest

from app.config import ScoringPolicy
from app.research.claims import validate_claims
from app.research.confidence import confidence_for_group
from app.research.normalisation import (
    EducationKind,
    PublicOfficeType,
    classify_education,
    classify_public_office,
    classify_relationship,
    normalise,
    normalise_degree_candidates,
)
from app.research.reconciliation import reconcile
from app.schemas import (
    EvidenceClaim,
    ExtractedClaim,
    ExtractionResponse,
    IdentityMatch,
    PersonSeed,
    ProfileField,
    SourceRecord,
    SourceType,
)


def source(
    identifier: str = "s1",
    *,
    source_type: SourceType = SourceType.employer,
    authority: float = 0.9,
    identity: float = 1,
) -> SourceRecord:
    domain = f"{identifier}.example"
    return SourceRecord(
        source_id=identifier,
        person_id="p1",
        requested_url=f"https://{domain}/profile",
        final_url=f"https://{domain}/profile",
        canonical_url=f"https://{domain}/profile",
        domain=domain,
        source_type=source_type,
        authority_score=authority,
        identity=IdentityMatch(score=identity, ambiguous=identity < 0.8),
        content_hash=identifier,
    )


def claim(
    identifier: str,
    source_id: str,
    field: ProfileField,
    value: str,
    **kwargs,
) -> EvidenceClaim:
    normalised, certainty = normalise(field, value)
    certainty = kwargs.pop("normalisation_certainty", certainty)
    identity_relevance = kwargs.pop("identity_relevance", 1)
    return EvidenceClaim(
        claim_id=identifier,
        person_id="p1",
        source_id=source_id,
        field=field,
        raw_value=value,
        normalised_value=normalised,
        normalisation_certainty=certainty,
        evidence_text=f"Jane Doe: {value}",
        subject_name="Jane Doe",
        identity_relevance=identity_relevance,
        extraction_model="fixture",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("raw", "degree_type", "subject", "reason"),
    [
        ("B.Tech", "Bachelor's Degree", None, None),
        ("B.Tech in Computer Science", "Bachelor's Degree", "Computer Science", None),
        ("Bachelor's in Chemical Engineering", "Bachelor's Degree", "Chemical Engineering", None),
        ("Master of Public Administration", "Master's Degree", "Public Administration", None),
        ("Master of Arts (MA)", "Master's Degree", None, None),
        ("Doctor of Philosophy (PhD)", "Doctoral Degree", None, None),
        ("BSc in Computer Science (BSc)", "Bachelor's Degree", "Computer Science", None),
        ("Doctor of Medicine", "Medical Degree", "medicine", None),
        ("Bachelor of Medicine, Bachelor of Surgery (MBBS)", "Medical Degree", "medicine", None),
        ("Juris Doctor", "Law Degree", "law", None),
        ("Bachelor of Laws", "Law Degree", "law", None),
        ("advanced postgraduate degree", "Postgraduate Degree", None, None),
        ("postgraduate diploma", "Postgraduate Diploma", None, None),
        ("diploma", "Diploma", None, None),
        ("degree", "Bachelor's Degree", None, "GENERIC_DEGREE_DEFAULT"),
        (
            "chemical engineering degree",
            "Bachelor's Degree",
            "chemical engineering",
            "GENERIC_DEGREE_DEFAULT",
        ),
    ],
)
def test_degree_titles_use_controlled_types_and_split_explicit_subjects(raw, degree_type, subject, reason):
    result = normalise_degree_candidates(raw)[0]

    assert result.value == degree_type
    assert result.subject == subject
    assert result.reason_code == reason


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("two degrees", EducationKind.ambiguous),
        ("several degrees", EducationKind.ambiguous),
        ("specialization course", EducationKind.training),
        ("graduate studies", EducationKind.ambiguous),
    ],
)
def test_vague_education_is_not_promoted_to_a_credential(raw, kind):
    assert classify_education(raw) == kind

    vague = claim("vague", "s1", ProfileField.degree_type, raw, fact_group="education")
    decision = (
        reconcile("p1", PersonSeed(full_name="Jane Doe"), [vague], [source()], ScoringPolicy())
        .records[0]
        .fields[ProfileField.degree_type]
    )

    assert decision.value is None
    assert decision.alternative_claim_ids == ["vague"]
    if kind == EducationKind.ambiguous:
        assert decision.review_required
        assert "AMBIGUOUS_EDUCATION" in decision.review_reason_codes


def test_degree_subject_is_derived_from_the_same_grounded_literal():
    text = "Jane Doe earned a B.Tech in Computer Science."
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=ProfileField.degree_type,
                value="B.Tech in Computer Science",
                evidence=text,
                subject_name="Jane Doe",
                fact_group="education-1",
            )
        ]
    )

    claims, reasons = validate_claims(
        response,
        PersonSeed(full_name="Jane Doe"),
        source(),
        text,
        "fixture",
    )

    assert not reasons
    by_field = {item.field: item for item in claims}
    assert by_field[ProfileField.degree_type].normalised_value == "Bachelor's Degree"
    assert by_field[ProfileField.subject].normalised_value == "computer science"
    assert by_field[ProfileField.subject].raw_value == "B.Tech in Computer Science"
    assert by_field[ProfileField.subject].source_claim_type == "derived_degree_subject"


@pytest.mark.parametrize(
    "raw",
    [
        "B.Tech in Computer Science and Master of Public Administration",
        "B.Tech in Computer Science, Master of Public Administration",
    ],
)
def test_compound_degree_subjects_stay_with_their_own_credential(raw):
    normalized = normalise_degree_candidates(raw)
    assert {item.value: item.subject for item in normalized} == {
        "Bachelor's Degree": "Computer Science",
        "Master's Degree": "Public Administration",
    }
    text = f"Jane Doe earned a {raw}."
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=ProfileField.degree_type,
                value=raw,
                evidence=text,
                subject_name="Jane Doe",
                fact_group="education-1",
            )
        ]
    )
    claims, reasons = validate_claims(
        response,
        PersonSeed(full_name="Jane Doe"),
        source(),
        text,
        "fixture",
    )

    assert not reasons
    records = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [source()], ScoringPolicy()).records
    assert {
        record.fields[ProfileField.degree_type].value: record.fields[ProfileField.subject].value
        for record in records
    } == {
        "Bachelor's Degree": "Computer Science",
        "Master's Degree": "Public Administration",
    }


def _role(source_id, organisation, title, *, observed, current=True):
    return [
        claim(
            f"{source_id}-org",
            source_id,
            ProfileField.organisation,
            organisation,
            fact_group=source_id,
            as_of_date=observed,
            is_current=current,
        ),
        claim(
            f"{source_id}-title",
            source_id,
            ProfileField.job_title,
            title,
            fact_group=source_id,
            as_of_date=observed,
            is_current=current,
        ),
    ]


def test_recent_primary_executive_beats_slightly_newer_board_role():
    sources = [source("executive"), source("board")]
    claims = [
        *_role("executive", "Example Holdings", "Chief Executive Officer", observed=date(2026, 9, 20)),
        *_role("board", "Example Trust", "Board Member", observed=date(2026, 9, 25)),
    ]

    record = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy(), date(2026, 9, 26)
    ).records[0]

    assert record.fields[ProfileField.organisation].value == "Example Holdings"
    assert record.fields[ProfileField.job_title].value == "Chief Executive Officer"


def test_undated_explicit_current_executive_beats_recent_board_role():
    sources = [
        source("executive", source_type=SourceType.first_party, authority=0.95),
        source("board", source_type=SourceType.directory, authority=0.65),
    ]
    claims = [
        *_role("executive", "Example Holdings", "Chief Executive Officer", observed=None),
        *_role("board", "Example Trust", "Board Member", observed=date(2026, 9, 25)),
    ]

    record = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy(), date(2026, 9, 26)
    ).records[0]

    assert record.fields[ProfileField.organisation].value == "Example Holdings"
    assert record.fields[ProfileField.job_title].value == "Chief Executive Officer"


def test_undated_explicit_current_government_office_beats_recent_party_role():
    sources = [
        source("government", source_type=SourceType.government, authority=0.95),
        source("party", source_type=SourceType.directory, authority=0.65),
    ]
    claims = [
        *_role("government", "Office of the Prime Minister", "Prime Minister", observed=None),
        *_role(
            "party",
            "Example National Party",
            "Party Leader",
            observed=date(2026, 9, 25),
        ),
    ]

    record = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy(), date(2026, 9, 26)
    ).records[0]

    assert record.fields[ProfileField.organisation].value == "Office of the Prime Minister"
    assert record.fields[ProfileField.job_title].value == "Prime Minister"


def test_recent_role_beats_stale_explicit_current_label():
    sources = [source("stale"), source("recent")]
    claims = [
        *_role("stale", "Old Company", "Chief Executive Officer", observed=date(2010, 1, 1)),
        *_role(
            "recent",
            "Current Company",
            "Managing Director",
            observed=date(2026, 9, 1),
            current=None,
        ),
    ]

    record = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy(), date(2026, 9, 26)
    ).records[0]

    assert record.fields[ProfileField.organisation].value == "Current Company"
    assert record.fields[ProfileField.job_title].value == "Managing Director"


def test_country_context_is_not_selected_without_a_seed_country():
    government = source("gov", source_type=SourceType.government, authority=0.95)
    claims = _role("gov", "Ghana", "President", observed=date(2026, 9, 1))

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [government], ScoringPolicy()).records[
        0
    ]

    organisation = record.fields[ProfileField.organisation]
    assert organisation.value is None
    assert organisation.alternative_claim_ids == ["gov-org"]
    assert record.fields[ProfileField.job_title].value == "President"


def test_encyclopedia_country_context_is_not_selected_as_an_organisation():
    encyclopedia = source(
        "encyclopedia",
        source_type=SourceType.encyclopedia,
        authority=0.6,
    )
    claims = _role("encyclopedia", "Ghana", "President", observed=date(2026, 9, 1))

    record = reconcile(
        "p1",
        PersonSeed(full_name="Jane Doe"),
        claims,
        [encyclopedia],
        ScoringPolicy(),
    ).records[0]

    assert record.fields[ProfileField.organisation].value is None
    assert record.fields[ProfileField.organisation].alternative_claim_ids == ["encyclopedia-org"]
    assert record.fields[ProfileField.job_title].value == "President"


def test_public_office_taxonomy_distinguishes_offices_from_private_agencies():
    assert classify_public_office("Constitutional Court", "Chief Justice") == PublicOfficeType.judiciary
    assert (
        classify_public_office("Federal Chancellery", "Chancellor", source_types={SourceType.government})
        == PublicOfficeType.head_of_government
    )
    assert (
        classify_public_office("Cabinet Office", "Minister", source_types={SourceType.government})
        == PublicOfficeType.ministerial
    )
    assert (
        classify_public_office(
            "Environmental Protection Agency",
            "Administrator",
            source_types={SourceType.government},
        )
        == PublicOfficeType.government_agency
    )
    assert (
        classify_public_office("Department of Physics", "Professor", source_types={SourceType.university})
        is None
    )
    assert classify_public_office("Acme Talent Agency", "CEO") is None
    assert classify_relationship("Acme Talent Agency", "CEO").value == "current_executive_role"
    assert (
        classify_public_office(
            "International Olympic Committee",
            "President",
            source_types={SourceType.encyclopedia},
        )
        is None
    )
    assert classify_public_office("FIFA", "President", source_types={SourceType.encyclopedia}) is None


def test_descriptive_biography_prose_is_not_selected_as_a_job_title():
    employer = source("employer")
    claims = [
        claim(
            "org",
            "employer",
            ProfileField.organisation,
            "Civic Initiative",
            fact_group="role",
            is_current=True,
        ),
        claim(
            "title",
            "employer",
            ProfileField.job_title,
            "passionate young activist in the Nigerian civic space",
            fact_group="role",
            is_current=True,
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [employer], ScoringPolicy()).records[0]
    decision = record.fields[ProfileField.job_title]

    assert decision.value is None
    assert decision.alternative_claim_ids == ["title"]
    assert "DESCRIPTIVE_JOB_TITLE" in decision.review_reason_codes
    assert "BELOW_SELECTION_FLOOR" in decision.review_reason_codes
    assert record.review_required
    assert "DESCRIPTIVE_JOB_TITLE" in record.review_reason_codes
    assert "BELOW_SELECTION_FLOOR" in record.review_reason_codes


def test_minimum_selected_confidence_suppresses_value_but_retains_evidence():
    weak_source = source("weak", source_type=SourceType.unknown, authority=0.25, identity=0.45)
    weak_claim = claim(
        "weak-org",
        "weak",
        ProfileField.organisation,
        "Possible Organisation",
        normalisation_certainty=0.1,
        identity_relevance=0.45,
        directness="ambiguous",
    )

    record = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), [weak_claim], [weak_source], ScoringPolicy()
    ).records[0]
    decision = record.fields[ProfileField.organisation]

    assert decision.confidence < 10
    assert decision.value is None
    assert decision.selected_claim_id is None
    assert decision.alternative_claim_ids == ["weak-org"]
    assert "BELOW_SELECTION_FLOOR" in decision.review_reason_codes
    assert record.review_required
    assert "BELOW_SELECTION_FLOOR" in record.review_reason_codes


def test_recency_discount_applies_only_to_time_sensitive_fields():
    policy = ScoringPolicy()
    official = source("official", source_type=SourceType.first_party, authority=0.95)
    recent = claim("recent", "official", ProfileField.organisation, "Example", as_of_date=date(2026, 9, 1))
    stale = claim("stale", "official", ProfileField.organisation, "Example", as_of_date=date(2010, 1, 1))
    education = claim("degree", "official", ProfileField.degree_type, "BSc", as_of_date=date(2010, 1, 1))
    sources = {official.source_id: official}

    recent_score = confidence_for_group([recent], [], sources, policy, date(2026, 9, 26))
    stale_score = confidence_for_group([stale], [], sources, policy, date(2026, 9, 26))
    education_score = confidence_for_group([education], [], sources, policy, date(2026, 9, 26))

    assert stale_score[0] < recent_score[0]
    assert stale_score[1]["time_sensitivity"] < 1
    assert education_score[1]["time_sensitivity"] == 1
