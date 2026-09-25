from datetime import date

import pytest

from app.config import ScoringPolicy, Settings
from app.input_validation import repair_mojibake
from app.research.claims import validate_claims
from app.research.confidence import confidence_for_group, profile_scores
from app.research.identity import assess_identity, effective_identity_scores
from app.research.normalisation import (
    EducationKind,
    classify_education,
    normalise,
    normalise_degree_candidates,
)
from app.research.reconciliation import reconcile
from app.schemas import (
    EvidenceClaim,
    ExtractedClaim,
    ExtractionResponse,
    FieldDecision,
    IdentityMatch,
    PersonSeed,
    ProfileField,
    SourceRecord,
    SourceType,
)


def source(
    identifier="s1",
    authority=0.9,
    domain="one.org",
    identity=1,
    content_hash="",
    source_type=SourceType.employer,
):
    return SourceRecord(
        source_id=identifier,
        person_id="p1",
        requested_url=f"https://{domain}/jane",
        final_url=f"https://{domain}/jane",
        canonical_url=f"https://{domain}/jane",
        domain=domain,
        source_type=source_type,
        authority_score=authority,
        identity=IdentityMatch(score=identity, ambiguous=identity < 0.8),
        content_hash=content_hash,
    )


def claim(identifier="c1", source_id="s1", field=ProfileField.organisation, value="Example", **kwargs):
    identity_relevance = kwargs.pop("identity_relevance", 1)
    normalised_value, normalisation_certainty = normalise(field, value)
    return EvidenceClaim(
        claim_id=identifier,
        person_id="p1",
        source_id=source_id,
        field=field,
        raw_value=value,
        normalised_value=normalised_value,
        normalisation_certainty=normalisation_certainty,
        evidence_text=f"Jane Doe works for {value}.",
        subject_name="Jane Doe",
        identity_relevance=identity_relevance,
        extraction_model="fake",
        **kwargs,
    )


def test_configuration_limits_secrets_and_production():
    settings = Settings(_env_file=None, OPENROUTER_API_KEY="private-test-key")
    assert "private-test-key" not in repr(settings)
    for params in (
        {"CHUNK_OVERLAP": 6000},
        {"MAX_SOURCES_PER_PERSON": 0},
        {"APP_ENV": "production"},
        {"FETCH_TIMEOUT_SECONDS": float("nan")},
    ):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **params)


def test_nested_policy_overrides_merge_defaults(monkeypatch):
    monkeypatch.setenv("SCORING__AUTHORITY__EMPLOYER", "0.82")
    settings = Settings(_env_file=None)
    assert settings.SCORING.authority[SourceType.employer] == 0.82
    assert settings.SCORING.authority[SourceType.government] == 0.95


def test_configuration_errors_do_not_echo_credentials():
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None, OPENROUTER_API_KEY="never-print-me", CHUNK_OVERLAP=6000)
    assert "never-print-me" not in str(error.value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BSc", "Bachelor's Degree"),
        ("B.Sc.", "Bachelor's Degree"),
        ("Bachelor of Science", "Bachelor's Degree"),
        ("BSc (Hons)", "Bachelor's Degree"),
        ("b tech", "Bachelor's Degree"),
        ("B.Tech", "Bachelor's Degree"),
        ("btech", "Bachelor's Degree"),
        ("bachelor s", "Bachelor's Degree"),
        ("bachelor degree", "Bachelor's Degree"),
        ("undergraduate degree", "Bachelor's Degree"),
        ("licence", "Bachelor's Degree"),
        ("laurea", "Bachelor's Degree"),
        ("diplômé", "Bachelor's Degree"),
        ("MSc", "Master's Degree"),
        ("MA", "Master's Degree"),
        ("MBA", "Master's Degree"),
        ("MPH", "Master's Degree"),
        ("m s", "Master's Degree"),
        ("master s", "Master's Degree"),
        ("master degree", "Master's Degree"),
        ("master 2", "Master's Degree"),
        ("mba degree", "Master's Degree"),
        ("executive mba", "Master's Degree"),
        ("maestría", "Master's Degree"),
        ("laurea magistrale", "Master's Degree"),
        ("PhD", "Doctoral Degree"),
        ("DPhil", "Doctoral Degree"),
        ("ph.d.", "Doctoral Degree"),
        ("phd degree", "Doctoral Degree"),
        ("doctorate", "Doctoral Degree"),
        ("promovierte", "Doctoral Degree"),
    ],
)
def test_degree_aliases(raw, expected):
    assert normalise(ProfileField.degree_type, raw)[0] == expected


def test_conservative_normalisation():
    assert normalise(ProfileField.organisation, " Example,   Ltd. ")[0] == "example"
    assert (
        normalise(ProfileField.job_title, "Senior Research Fellow")[0]
        != normalise(ProfileField.job_title, "Research Fellow")[0]
    )
    assert normalise(ProfileField.degree_type, "Basket weaving")[0] == "Basket weaving"


def test_conservative_display_formatting_preserves_acronyms_and_normalizes_connectors():
    claims = [
        claim(
            "university",
            field=ProfileField.university_name,
            value="MIT UNIVERSITY OF TECHNOLOGY",
            fact_group="education",
        )
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [source()], ScoringPolicy()).records[0]

    assert record.fields[ProfileField.university_name].value == "MIT University of Technology"


def test_unicode_cleanup_is_narrow_and_preserves_valid_international_names():
    assert repair_mojibake("JosÃ© NicolÃ¡s YamandÃº") == "José Nicolás Yamandú"
    assert repair_mojibake("José Nicolás Yamandú") == "José Nicolás Yamandú"
    assert repair_mojibake("François Łukasz") == "François Łukasz"


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("PMP certification", EducationKind.certification),
        ("senior executive programme", EducationKind.executive_education),
        ("senior executive programs", EducationKind.executive_education),
        ("honorary doctorate", EducationKind.honorary_degree),
        ("postdoctoral work", EducationKind.postdoctoral),
        ("doctoral candidate", EducationKind.ongoing_study),
        ("ongoing study", EducationKind.ongoing_study),
        ("graduate training", EducationKind.training),
        ("executive MBA", EducationKind.academic_degree),
    ],
)
def test_education_classification_precedes_degree_aliases(value, kind):
    assert classify_education(value) == kind


def test_generic_and_unknown_degrees_keep_normalisation_meaning():
    generic = normalise_degree_candidates("degree")[0]
    unknown = normalise_degree_candidates("Diplôme supérieur inconnu")[0]

    assert generic.value == "Bachelor's Degree"
    assert generic.reason_code == "GENERIC_DEGREE_DEFAULT"
    assert generic.certainty < 1
    assert unknown.value == "Diplôme supérieur inconnu"
    assert unknown.reason_code == "AMBIGUOUS_EDUCATION"

    evidence_source = source(
        "official", authority=0.95, domain="official.example", source_type=SourceType.first_party
    )
    generic_profile = reconcile(
        "p1",
        PersonSeed(full_name="Jane Doe"),
        [
            claim("name", "official", field=ProfileField.full_name, value="Jane Doe"),
            claim(
                "generic",
                "official",
                field=ProfileField.degree_type,
                value="degree",
                fact_group="education",
            ),
        ],
        [evidence_source],
        ScoringPolicy(),
    )
    generic_decision = generic_profile.records[0].fields[ProfileField.degree_type]
    assert generic_decision.scoring_components["normalisation_reason"] == "GENERIC_DEGREE_DEFAULT"

    unknown_profile = reconcile(
        "p1",
        PersonSeed(full_name="Jane Doe"),
        [
            claim("name", "official", field=ProfileField.full_name, value="Jane Doe"),
            claim(
                "unknown",
                "official",
                field=ProfileField.degree_type,
                value="Diplôme supérieur inconnu",
                fact_group="education",
            ),
        ],
        [evidence_source],
        ScoringPolicy(),
    )
    unknown_decision = unknown_profile.records[0].fields[ProfileField.degree_type]
    assert unknown_decision.value == "Diplôme supérieur inconnu"
    assert unknown_decision.review_required
    assert not unknown_profile.records[0].review_required


def test_compound_degree_claim_expands_before_reconciliation_and_keeps_raw_evidence():
    text = "Jane Doe obtuvo una maestría y un doctorado en Example University."
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field=ProfileField.degree_type,
                value="maestría y un doctorado",
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
        "fake",
    )

    assert not reasons
    assert {item.normalised_value for item in claims} == {"Master's Degree", "Doctoral Degree"}
    assert {item.raw_value for item in claims} == {"maestría y un doctorado"}
    profile = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [source()], ScoringPolicy())
    assert {record.fields[ProfileField.degree_type].value for record in profile.records} == {
        "Master's Degree",
        "Doctoral Degree",
    }


def test_non_degree_education_stays_in_evidence_without_creating_degree_rows():
    values = [
        "PMP certification",
        "senior executive programme",
        "honorary doctorate",
        "postdoctoral work",
        "doctoral candidate",
        "graduate training",
    ]
    claims = [
        claim(
            f"non-degree-{index}",
            field=ProfileField.degree_type,
            value=value,
            fact_group=f"education-{index}",
        )
        for index, value in enumerate(values)
    ]

    profile = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [source()], ScoringPolicy())

    assert len(profile.records) == 1
    degree = profile.records[0].fields[ProfileField.degree_type]
    assert degree.value is None
    assert set(degree.alternative_claim_ids) == {item.claim_id for item in claims}
    assert not degree.review_required
    assert not profile.records[0].review_required


def test_non_degree_claim_cannot_displace_academic_degree_in_mixed_source_group():
    claims = [
        claim(
            "academic",
            field=ProfileField.degree_type,
            value="MSc",
            fact_group="education",
        ),
        claim(
            "certification",
            field=ProfileField.degree_type,
            value="PMP certification",
            fact_group="education",
        ),
        claim(
            "university",
            field=ProfileField.university_name,
            value="Example University",
            fact_group="education",
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [source()], ScoringPolicy()).records[0]

    assert record.fields[ProfileField.degree_type].value == "Master's Degree"
    assert record.fields[ProfileField.degree_type].alternative_claim_ids == ["certification"]


def test_normalized_equivalent_masters_merge_with_matching_context():
    sources = [source(f"s{index}", domain=f"{index}.edu") for index in range(1, 4)]
    claims = []
    for index, degree in enumerate(("Master's", "master s", "MA APP"), start=1):
        group = f"credential-{index}"
        claims.extend(
            [
                claim(
                    f"d{index}",
                    f"s{index}",
                    field=ProfileField.degree_type,
                    value=degree,
                    fact_group=group,
                ),
                claim(
                    f"u{index}",
                    f"s{index}",
                    field=ProfileField.university_name,
                    value="Example University",
                    fact_group=group,
                ),
                claim(
                    f"sub{index}",
                    f"s{index}",
                    field=ProfileField.subject,
                    value="public policy",
                    fact_group=group,
                ),
            ]
        )

    profile = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy())

    assert len(profile.records) == 1
    assert profile.records[0].fields[ProfileField.degree_type].value == "Master's Degree"
    assert profile.records[0].fields[ProfileField.subject].value == "Public Policy"


def test_two_explicit_same_level_degrees_with_distinct_context_stay_separate():
    claims = []
    for index, (university, subject) in enumerate(
        (("University One", "Economics"), ("University Two", "Engineering")), start=1
    ):
        group = f"credential-{index}"
        claims.extend(
            [
                claim(
                    f"d{index}",
                    field=ProfileField.degree_type,
                    value="MSc",
                    fact_group=group,
                ),
                claim(
                    f"u{index}",
                    field=ProfileField.university_name,
                    value=university,
                    fact_group=group,
                ),
                claim(
                    f"s{index}",
                    field=ProfileField.subject,
                    value=subject,
                    fact_group=group,
                ),
            ]
        )

    records = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [source()], ScoringPolicy()).records

    assert len(records) == 2
    assert {record.fields[ProfileField.university_name].value for record in records} == {
        "University One",
        "University Two",
    }


def test_missing_optional_fields_and_one_weak_optional_field_do_not_escalate_record():
    strong = source("strong", authority=0.95, domain="official.example", source_type=SourceType.first_party)
    weak = source("weak", authority=0.9, domain="secondary.example")
    claims = [claim("name", "strong", field=ProfileField.full_name, value="Jane Doe")]
    clean = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [strong], ScoringPolicy()).records[0]

    assert not clean.fields[ProfileField.subject].review_required
    assert not clean.review_required

    claims.append(
        claim(
            "subject",
            "weak",
            field=ProfileField.subject,
            value="chemical engineering",
            directness="ambiguous",
        )
    )
    one_weak = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), claims, [strong, weak], ScoringPolicy()
    ).records[0]

    assert one_weak.fields[ProfileField.subject].value == "Chemical Engineering"
    assert one_weak.fields[ProfileField.subject].review_required
    assert not one_weak.review_required


def test_weak_name_conflict_stays_field_review_without_escalating_record():
    strong = source(
        "strong",
        authority=0.95,
        domain="official.example",
        source_type=SourceType.first_party,
    )
    weak = source(
        "weak",
        authority=0.25,
        domain="aggregator.example",
        identity=0.55,
        source_type=SourceType.aggregator,
    )
    claims = [
        claim("exact-name", "strong", field=ProfileField.full_name, value="Jane Doe"),
        claim(
            "weak-variant",
            "weak",
            field=ProfileField.full_name,
            value="Jane A. Doe",
            directness="ambiguous",
        ),
    ]

    record = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), claims, [strong, weak], ScoringPolicy()
    ).records[0]

    name = record.fields[ProfileField.full_name]
    assert name.value == "Jane Doe"
    assert name.conflicting_claim_ids == ["weak-variant"]
    assert name.review_required
    assert name.scoring_components["conflict_strength"] < 0.5
    assert not record.review_required


def test_identity_name_only_is_ambiguous_and_clues_help():
    seed = PersonSeed(full_name="Jane Doe", country="Ghana", organisation="Example Foundation")
    policy = ScoringPolicy()
    alone = assess_identity(seed, "Jane Doe is an author", policy)
    known = assess_identity(seed, "Jane Doe, Example Foundation, Ghana", policy)
    assert alone.ambiguous and alone.score < known.score
    assert not known.ambiguous
    assert assess_identity(seed, "Janet Doe from Ghana", policy).rejected
    assert assess_identity(seed, "Jane Doe in Ghana", policy, subject_name="Janet Doe").rejected


def test_claims_need_literal_evidence_values_and_target_subject():
    text = "Jane Doe works at Example Foundation in Ghana."
    seed = PersonSeed(full_name="Jane Doe")
    valid = ExtractedClaim(
        field="organisation", value="Example Foundation", evidence=text, subject_name="Jane Doe"
    )
    fabricated = valid.model_copy(update={"value": "Imagined Foundation"})
    invented_evidence = valid.model_copy(update={"evidence": "A completely invented quotation."})
    wrong_person = valid.model_copy(update={"subject_name": "John Doe"})
    result, codes = validate_claims(
        ExtractionResponse(claims=[valid, fabricated, invented_evidence, wrong_person]),
        seed,
        source(),
        text,
        "fake",
    )
    assert len(result) == 1
    assert result[0].raw_value == "Example Foundation" and result[0].source_id == "s1"
    assert set(codes) == {"UNGROUNDED_VALUE", "UNGROUNDED_EVIDENCE", "CLAIM_SUBJECT_MISMATCH"}


def test_strict_claim_output_rejects_schema_mismatch():
    with pytest.raises(ValueError):
        ExtractionResponse.model_validate({"claims": [{"field": "secret_field", "value": "x"}]})
    with pytest.raises(ValueError):
        ExtractionResponse.model_validate({"claims": [], "biography": "unrequested"})


def test_claim_dates_need_a_complete_date_in_the_excerpt():
    text = "Jane Doe joined Example in 2020."
    response = ExtractionResponse(
        claims=[
            ExtractedClaim(
                field="organisation",
                value="Example",
                evidence=text,
                subject_name="Jane Doe",
                as_of_date=date(2020, 1, 1),
            )
        ]
    )
    accepted, codes = validate_claims(response, PersonSeed(full_name="Jane Doe"), source(), text, "fake")
    assert not accepted and "UNGROUNDED_CLAIM_DATE" in codes


def test_independent_corroboration_monotone_and_duplicates_do_not_help():
    policy = ScoringPolicy()
    first, second = claim(), claim("c2", "s2")
    sources = {"s1": source(), "s2": source("s2", domain="two.org")}
    one = confidence_for_group([first], [], sources, policy)[0]
    two = confidence_for_group([first, second], [], sources, policy)[0]
    assert two > one
    sources["s2"].domain = "one.org"
    assert confidence_for_group([first, second], [], sources, policy)[0] == one
    sources["s2"].domain = "two.org"
    sources["s1"].content_hash = sources["s2"].content_hash = "identical"
    assert confidence_for_group([first, second], [], sources, policy)[0] == one


def test_authority_conflict_bounds_and_identity():
    policy = ScoringPolicy()
    sources = {"s1": source(authority=0.3), "s2": source("s2", domain="two.org")}
    c = claim()
    low = confidence_for_group([c], [], sources, policy)[0]
    sources["s1"].authority_score = 0.95
    strong = confidence_for_group([c], [], sources, policy)[0]
    conflict = confidence_for_group([c], [claim("c2", "s2", value="Different")], sources, policy)[0]
    assert 0 <= low < strong <= 100 and conflict < strong
    sources["s1"].identity.score = 0.55
    assert confidence_for_group([c], [], sources, policy)[0] < strong


def test_role_recency_matters_education_recency_does_not():
    sources = {"s1": source()}
    policy = ScoringPolicy()
    old = claim(as_of_date=date(2000, 1, 1))
    recent = claim(as_of_date=date(2026, 1, 1))
    today = date(2026, 9, 15)
    assert (
        confidence_for_group([old], [], sources, policy, today)[0]
        < confidence_for_group([recent], [], sources, policy, today)[0]
    )
    old.field = recent.field = ProfileField.university_name
    assert (
        confidence_for_group([old], [], sources, policy, today)[0]
        == confidence_for_group([recent], [], sources, policy, today)[0]
    )


def test_profile_confidence_is_separate_from_coverage():
    fields = {field: FieldDecision() for field in ProfileField}
    fields[ProfileField.full_name] = FieldDecision(value="Jane Doe", confidence=95)
    confidence, coverage = profile_scores(fields, ScoringPolicy())
    assert confidence == 95 and coverage == 14.29
    fields[ProfileField.organisation] = FieldDecision(value="Example", confidence=45)
    assert profile_scores(fields, ScoringPolicy()) == (70, 28.57)


def test_reconciliation_preserves_conflicts_and_missing_fields():
    first, other = claim(), claim("c2", "s2", value="Other")
    profile = reconcile(
        "p1",
        PersonSeed(full_name="Jane Doe"),
        [first, other],
        [source(), source("s2", authority=0.9, domain="two.org")],
        ScoringPolicy(),
    )
    field = profile.fields[ProfileField.organisation]
    assert field.value == "Example" and field.review_required and field.conflicting_claim_ids == ["c2"]
    assert "SOURCE_CONFLICT" in field.review_reason_codes
    assert (
        profile.fields[ProfileField.full_name].value is None
    )  # Never copy the seed into an unsupported result.


def test_historical_employer_is_not_presented_as_current_or_conflict():
    historical = claim("old", value="Former Employer", is_current=False, end_date=date(2020, 1, 1))
    current = claim("new", value="Current Employer", is_current=True)
    profile = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), [historical, current], [source()], ScoringPolicy()
    )
    decision = profile.fields[ProfileField.organisation]
    assert decision.value == "Current Employer" and decision.alternative_claim_ids == ["old"]
    assert not decision.conflicting_claim_ids


def test_multiple_education_records_are_preserved_and_not_mixed():
    degree1 = claim("d1", field=ProfileField.degree_type, value="BSc", fact_group="education-1")
    degree2 = claim("d2", "s2", field=ProfileField.degree_type, value="MSc", fact_group="education-2")
    uni2 = claim(
        "u2", "s2", field=ProfileField.university_name, value="University Two", fact_group="education-2"
    )
    profile = reconcile(
        "p1",
        PersonSeed(full_name="Jane Doe"),
        [degree1, degree2, uni2],
        [source(), source("s2", authority=0.4, domain="two.org")],
        ScoringPolicy(),
    )
    assert profile.fields[ProfileField.degree_type].value == "Bachelor's Degree"
    assert not profile.fields[ProfileField.degree_type].conflicting_claim_ids
    assert profile.fields[ProfileField.university_name].value is None
    assert profile.fields[ProfileField.university_name].alternative_claim_ids == ["u2"]


def test_wrong_person_authoritative_source_cannot_win():
    wrong = source(identity=0.1, authority=1)
    profile = reconcile("p1", PersonSeed(full_name="Jane Doe"), [claim()], [wrong], ScoringPolicy())
    assert all(f.value is None for f in profile.fields.values())
    assert profile.profile_confidence == profile.coverage == 0


def test_ungrouped_degree_component_cannot_be_attached_to_selected_record():
    degree = claim("degree", field=ProfileField.degree_type, value="BSc", fact_group="first-degree")
    university = claim("university", "s2", field=ProfileField.university_name, value="University Two")
    profile = reconcile(
        "p1",
        PersonSeed(full_name="Jane Doe"),
        [degree, university],
        [source(), source("s2", authority=0.4, domain="two.org")],
        ScoringPolicy(),
    )
    assert profile.fields[ProfileField.degree_type].value == "Bachelor's Degree"
    assert profile.fields[ProfileField.university_name].value is None
    assert "UNPAIRED_FACT" in profile.fields[ProfileField.university_name].review_reason_codes


def test_multi_source_person_assembles_one_record_with_field_provenance():
    sources = [source(), source("s2", domain="two.edu", source_type=SourceType.university)]
    claims = [
        claim("name", field=ProfileField.full_name, value="Jane Doe"),
        claim(
            "org",
            field=ProfileField.organisation,
            value="Example Foundation",
            fact_group="current-role",
            is_current=True,
        ),
        claim(
            "title",
            field=ProfileField.job_title,
            value="Programme Director",
            fact_group="current-role",
            is_current=True,
        ),
        claim(
            "uni",
            "s2",
            field=ProfileField.university_name,
            value="Example University",
            fact_group="degree-1",
        ),
        claim(
            "degree",
            "s2",
            field=ProfileField.degree_type,
            value="BSc",
            fact_group="degree-1",
        ),
        claim(
            "subject",
            "s2",
            field=ProfileField.subject,
            value="Economics",
            fact_group="degree-1",
        ),
    ]

    profile = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy())
    assert len(profile.records) == 1
    record = profile.records[0]
    assert record.fields[ProfileField.organisation].value == "Example Foundation"
    assert record.fields[ProfileField.degree_type].value == "Bachelor's Degree"
    assert record.fields[ProfileField.organisation].supporting_source_ids == ["s1"]
    assert record.fields[ProfileField.university_name].supporting_source_ids == ["s2"]
    assert record.fields[ProfileField.university_name].sources == ["https://two.edu/jane"]


def test_same_credential_many_sources_merges_and_corroborates_field():
    sources = [
        source("s1", domain="one.edu", source_type=SourceType.university),
        source("s2", domain="two.edu", source_type=SourceType.university),
        source("s3", domain="three.edu", source_type=SourceType.university),
    ]
    claims = []
    university_names = ["The University of Example", "Example University", "Example University"]
    for index, university in enumerate(university_names, start=1):
        source_id = f"s{index}"
        group = f"degree-{index}"
        claims.extend(
            [
                claim(
                    f"u{index}",
                    source_id,
                    field=ProfileField.university_name,
                    value=university,
                    fact_group=group,
                ),
                claim(
                    f"d{index}",
                    source_id,
                    field=ProfileField.degree_type,
                    value="BSc",
                    fact_group=group,
                ),
            ]
        )

    profile = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy())
    single = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims[:2], sources[:1], ScoringPolicy())

    assert len(profile.records) == 1
    degree = profile.records[0].fields[ProfileField.degree_type]
    university = profile.records[0].fields[ProfileField.university_name]
    assert degree.value == "Bachelor's Degree"
    assert set(degree.supporting_source_ids) == {"s1", "s2", "s3"}
    assert set(university.supporting_source_ids) == {"s1", "s2", "s3"}
    assert university.scoring_components["independent_domains"] == 3
    assert university.confidence > single.records[0].fields[ProfileField.university_name].confidence


def test_three_distinct_degrees_create_three_record_scopes():
    sources = [
        source("s1", domain="one.edu"),
        source("s2", domain="two.edu"),
        source("s3", domain="three.edu"),
    ]
    credentials = [
        ("s1", "BSc", "University One", "Economics"),
        ("s2", "MSc", "University Two", "Policy"),
        ("s3", "PhD", "University Three", "History"),
    ]
    claims = []
    for source_id, degree, university, subject in credentials:
        group = f"credential-{source_id}"
        claims.extend(
            [
                claim(
                    f"{source_id}-d",
                    source_id,
                    field=ProfileField.degree_type,
                    value=degree,
                    fact_group=group,
                ),
                claim(
                    f"{source_id}-u",
                    source_id,
                    field=ProfileField.university_name,
                    value=university,
                    fact_group=group,
                ),
                claim(
                    f"{source_id}-s",
                    source_id,
                    field=ProfileField.subject,
                    value=subject,
                    fact_group=group,
                ),
            ]
        )

    profile = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy())

    assert len(profile.records) == 3
    assert {record.fields[ProfileField.degree_type].value for record in profile.records} == {
        "Bachelor's Degree",
        "Master's Degree",
        "Doctoral Degree",
    }
    for record in profile.records:
        education_sources = {
            source_id
            for field_name in (
                ProfileField.university_name,
                ProfileField.degree_type,
                ProfileField.subject,
            )
            for source_id in record.fields[field_name].supporting_source_ids
        }
        assert len(education_sources) == 1


def test_latest_employment_is_selected_as_an_organisation_title_pair():
    sources = [source("old", domain="old.org"), source("new", domain="new.org")]
    claims = [
        claim(
            "old-org",
            "old",
            value="Organisation A",
            fact_group="old-role",
            is_current=False,
            end_date=date(2020, 1, 1),
        ),
        claim(
            "old-title",
            "old",
            field=ProfileField.job_title,
            value="Role A",
            fact_group="old-role",
            is_current=False,
            end_date=date(2020, 1, 1),
        ),
        claim(
            "new-org",
            "new",
            value="Organisation B",
            fact_group="new-role",
            is_current=True,
            as_of_date=date(2026, 1, 1),
        ),
        claim(
            "new-title",
            "new",
            field=ProfileField.job_title,
            value="Role B",
            fact_group="new-role",
            is_current=True,
            as_of_date=date(2026, 1, 1),
        ),
    ]

    profile = reconcile(
        "p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy(), date(2026, 9, 22)
    )

    organisation = profile.records[0].fields[ProfileField.organisation]
    title = profile.records[0].fields[ProfileField.job_title]
    assert (organisation.value, title.value) == ("Organisation B", "Role B")
    assert organisation.supporting_source_ids == title.supporting_source_ids == ["new"]
    assert organisation.alternative_claim_ids == ["old-org"]
    assert title.alternative_claim_ids == ["old-title"]


def test_equally_current_role_conflict_never_mixes_relationship_components():
    sources = [source("a", domain="a.gov"), source("b", domain="b.gov")]
    claims = [
        claim("a-org", "a", value="Office A", fact_group="role-a", is_current=True),
        claim(
            "a-title",
            "a",
            field=ProfileField.job_title,
            value="Minister A",
            fact_group="role-a",
            is_current=True,
        ),
        claim("b-org", "b", value="Office B", fact_group="role-b", is_current=True),
        claim(
            "b-title",
            "b",
            field=ProfileField.job_title,
            value="Minister B",
            fact_group="role-b",
            is_current=True,
        ),
    ]

    profile = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy())
    organisation = profile.records[0].fields[ProfileField.organisation]
    title = profile.records[0].fields[ProfileField.job_title]

    assert organisation.review_required and title.review_required
    assert "CURRENT_ROLE_CONFLICT" in organisation.review_reason_codes
    assert ({organisation.value, title.value} == {"Office A", "Minister A"}) or (
        {organisation.value, title.value} == {"Office B", "Minister B"}
    )


def test_government_office_is_supported_without_company_assumptions():
    government = source(
        "gov",
        authority=0.95,
        domain="cabinet.gov.example",
        source_type=SourceType.government,
    )
    claims = [
        claim(
            "gov-org",
            "gov",
            value="Ministry of Science",
            fact_group="current-office",
            is_current=True,
        ),
        claim(
            "gov-title",
            "gov",
            field=ProfileField.job_title,
            value="Minister of Science",
            fact_group="current-office",
            is_current=True,
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [government], ScoringPolicy()).records[
        0
    ]

    assert record.fields[ProfileField.organisation].value == "Ministry of Science"
    assert record.fields[ProfileField.job_title].value == "Minister of Science"
    assert record.fields[ProfileField.profile_link].value == government.final_url


def test_current_government_office_outranks_party_role_with_equal_currentness():
    sources = [
        source("gov", domain="cabinet.gov.example", source_type=SourceType.government),
        source("party", domain="party.example"),
    ]
    claims = [
        claim("gov-org", "gov", value="Office of the Prime Minister", fact_group="office", is_current=True),
        claim(
            "gov-title",
            "gov",
            field=ProfileField.job_title,
            value="Prime Minister",
            fact_group="office",
            is_current=True,
        ),
        claim("party-org", "party", value="Example National Party", fact_group="party", is_current=True),
        claim(
            "party-title",
            "party",
            field=ProfileField.job_title,
            value="Party Leader",
            fact_group="party",
            is_current=True,
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy()).records[0]

    assert record.fields[ProfileField.organisation].value == "Office of the Prime Minister"
    assert record.fields[ProfileField.job_title].value == "Prime Minister"
    assert not record.fields[ProfileField.organisation].conflicting_claim_ids
    assert (
        record.fields[ProfileField.organisation].scoring_components["relationship_type"]
        == "current_government_office"
    )


def test_primary_executive_role_outranks_board_membership():
    sources = [source("company", domain="company.example"), source("board", domain="board.example")]
    claims = [
        claim("company-org", "company", value="Example Holdings", fact_group="executive", is_current=True),
        claim(
            "company-title",
            "company",
            field=ProfileField.job_title,
            value="Chief Executive Officer",
            fact_group="executive",
            is_current=True,
        ),
        claim("board-org", "board", value="Example Trust", fact_group="board", is_current=True),
        claim(
            "board-title",
            "board",
            field=ProfileField.job_title,
            value="Board Member",
            fact_group="board",
            is_current=True,
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy()).records[0]

    assert record.fields[ProfileField.organisation].value == "Example Holdings"
    assert record.fields[ProfileField.job_title].value == "Chief Executive Officer"
    assert (
        record.fields[ProfileField.job_title].scoring_components["relationship_type"]
        == "current_executive_role"
    )


def test_country_is_not_selected_as_government_organisation_or_replaced_with_invented_office():
    government = source("gov", domain="government.example", source_type=SourceType.government)
    claims = [
        claim("country", "gov", value="Ghana", fact_group="office", is_current=True),
        claim(
            "president",
            "gov",
            field=ProfileField.job_title,
            value="President",
            fact_group="office",
            is_current=True,
        ),
    ]

    record = reconcile(
        "p1", PersonSeed(full_name="Jane Doe", country="Ghana"), claims, [government], ScoringPolicy()
    ).records[0]

    organisation = record.fields[ProfileField.organisation]
    assert organisation.value is None
    assert organisation.alternative_claim_ids == ["country"]
    assert record.fields[ProfileField.job_title].value == "President"
    assert record.review_required
    assert "CURRENT_ROLE_RELATIONSHIP_UNCERTAIN" in record.review_reason_codes


def test_public_residence_is_not_selected_as_the_office_organisation():
    government = source("gov", domain="government.example", source_type=SourceType.government)
    claims = [
        claim(
            "residence",
            "gov",
            value="Presidential Palace",
            fact_group="office",
            is_current=True,
        ),
        claim(
            "president",
            "gov",
            field=ProfileField.job_title,
            value="President",
            fact_group="office",
            is_current=True,
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [government], ScoringPolicy()).records[
        0
    ]

    organisation = record.fields[ProfileField.organisation]
    assert organisation.value is None
    assert organisation.alternative_claim_ids == ["residence"]
    assert "INVALID_ORGANISATION_VALUE" in organisation.review_reason_codes
    assert record.fields[ProfileField.job_title].value == "President"
    assert "CURRENT_ROLE_RELATIONSHIP_UNCERTAIN" in record.review_reason_codes


def test_corporate_president_is_classified_as_executive_not_government():
    claims = [
        claim("org", value="Example Corporation", fact_group="role", is_current=True),
        claim(
            "title",
            field=ProfileField.job_title,
            value="President",
            fact_group="role",
            is_current=True,
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, [source()], ScoringPolicy()).records[0]

    assert (
        record.fields[ProfileField.job_title].scoring_components["relationship_type"]
        == "current_executive_role"
    )


def test_representative_link_uses_selected_field_contribution_before_authority():
    sources = [
        source("main", authority=0.9, domain="main.org"),
        source("degree", authority=0.95, domain="degree.edu", source_type=SourceType.university),
    ]
    claims = [
        claim("name", "main", field=ProfileField.full_name, value="Jane Doe"),
        claim("org", "main", value="Example", fact_group="role", is_current=True),
        claim(
            "title",
            "main",
            field=ProfileField.job_title,
            value="Director",
            fact_group="role",
            is_current=True,
        ),
        claim(
            "main-uni",
            "main",
            field=ProfileField.university_name,
            value="Example University",
            fact_group="degree-main",
        ),
        claim(
            "degree-uni",
            "degree",
            field=ProfileField.university_name,
            value="Example University",
            fact_group="degree-source",
        ),
        claim(
            "degree-type",
            "degree",
            field=ProfileField.degree_type,
            value="BSc",
            fact_group="degree-source",
        ),
        claim(
            "degree-subject",
            "degree",
            field=ProfileField.subject,
            value="Economics",
            fact_group="degree-source",
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy()).records[0]
    link = record.fields[ProfileField.profile_link]

    assert link.value == "https://main.org/jane"
    assert link.scoring_components["selected_field_contributions"] == 4


def test_each_education_record_can_choose_a_different_representative_link():
    sources = [
        source("common", authority=0.8, domain="person.org"),
        source("bachelor", authority=0.95, domain="bachelor.edu", source_type=SourceType.university),
        source("master", authority=0.95, domain="master.edu", source_type=SourceType.university),
    ]
    claims = [
        claim("name", "common", field=ProfileField.full_name, value="Jane Doe"),
        claim("org", "common", value="Example", fact_group="role", is_current=True),
        claim(
            "title",
            "common",
            field=ProfileField.job_title,
            value="Director",
            fact_group="role",
            is_current=True,
        ),
    ]
    for source_id, degree, university, subject in (
        ("bachelor", "BSc", "Bachelor University", "Economics"),
        ("master", "MSc", "Master University", "Policy"),
    ):
        claims.extend(
            [
                claim(
                    f"{source_id}-u",
                    source_id,
                    field=ProfileField.university_name,
                    value=university,
                    fact_group=source_id,
                ),
                claim(
                    f"{source_id}-d",
                    source_id,
                    field=ProfileField.degree_type,
                    value=degree,
                    fact_group=source_id,
                ),
                claim(
                    f"{source_id}-s",
                    source_id,
                    field=ProfileField.subject,
                    value=subject,
                    fact_group=source_id,
                ),
            ]
        )

    records = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy()).records
    links = {
        record.fields[ProfileField.degree_type].value: record.fields[ProfileField.profile_link].value
        for record in records
    }

    assert links == {
        "Bachelor's Degree": "https://bachelor.edu/jane",
        "Master's Degree": "https://master.edu/jane",
    }


def test_name_only_identity_strengthens_only_through_independent_context_agreement():
    sources = {
        "s1": source("s1", domain="one.org", identity=0.55),
        "s2": source("s2", domain="two.org", identity=0.55),
    }
    claims = [
        claim("n1", "s1", field=ProfileField.full_name, value="Jane Doe", identity_relevance=0.55),
        claim("o1", "s1", value="Example Foundation", identity_relevance=0.55),
        claim("n2", "s2", field=ProfileField.full_name, value="Jane Doe", identity_relevance=0.55),
        claim("o2", "s2", value="Example Foundation", identity_relevance=0.55),
    ]
    policy = ScoringPolicy()
    effective = effective_identity_scores(claims, sources, policy)
    single = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims[:2], [sources["s1"]], policy)
    corroborated = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, list(sources.values()), policy)

    assert sources["s1"].identity.score == 0.55
    assert effective["s1"] > policy.identity_review_threshold
    single_name = single.records[0].fields[ProfileField.full_name]
    corroborated_name = corroborated.records[0].fields[ProfileField.full_name]
    assert corroborated_name.confidence > single_name.confidence
    assert "IDENTITY_AMBIGUITY" not in corroborated_name.review_reason_codes


def test_identity_ambiguity_still_escalates_the_record():
    ambiguous_source = source("ambiguous", authority=0.9, identity=0.55)
    profile = reconcile(
        "p1",
        PersonSeed(full_name="Jane Doe"),
        [
            claim(
                "name",
                "ambiguous",
                field=ProfileField.full_name,
                value="Jane Doe",
                identity_relevance=0.55,
            )
        ],
        [ambiguous_source],
        ScoringPolicy(),
    )

    assert profile.records[0].review_required
    assert "IDENTITY_AMBIGUITY" in profile.records[0].review_reason_codes


def test_name_only_mirrors_do_not_strengthen_identity():
    sources = {
        "s1": source("s1", domain="one.org", identity=0.55, content_hash="mirror"),
        "s2": source("s2", domain="two.org", identity=0.55, content_hash="mirror"),
    }
    claims = [
        claim("o1", "s1", value="Example Foundation", identity_relevance=0.55),
        claim("o2", "s2", value="Example Foundation", identity_relevance=0.55),
    ]

    scores = effective_identity_scores(claims, sources, ScoringPolicy())

    assert scores == {"s1": 0.55, "s2": 0.55}


def test_exact_name_from_independent_authoritative_sources_gets_field_only_uplift():
    policy = ScoringPolicy(review_threshold=60)
    sources = {
        "s1": source("s1", authority=0.9, domain="one.gov", identity=0.55),
        "s2": source("s2", authority=0.9, domain="two.edu", identity=0.55),
    }
    names = [
        claim("n1", "s1", field=ProfileField.full_name, value="Jane Doe", identity_relevance=0.55),
        claim("n2", "s2", field=ProfileField.full_name, value="Jane Doe", identity_relevance=0.55),
    ]

    one = confidence_for_group(names[:1], [], sources, policy)
    two = confidence_for_group(names, [], sources, policy)

    assert one[0] < policy.review_threshold < two[0]
    assert two[1]["name_identity_floor"] > policy.identity_review_threshold
    assert sources["s1"].identity.score == sources["s2"].identity.score == 0.55


def test_weak_source_quantity_cannot_take_representative_link_from_reliable_source():
    sources = [
        source(
            "weak",
            authority=0.45,
            domain="profiles.example",
            source_type=SourceType.aggregator,
        ),
        source(
            "official",
            authority=0.9,
            domain="university.example",
            source_type=SourceType.university,
        ),
    ]
    claims = [
        claim("name", "weak", field=ProfileField.full_name, value="Jane Doe"),
        claim("org", "weak", value="Example Office", fact_group="role", is_current=True),
        claim(
            "title",
            "weak",
            field=ProfileField.job_title,
            value="Director",
            fact_group="role",
            is_current=True,
        ),
        claim(
            "degree",
            "official",
            field=ProfileField.degree_type,
            value="BSc",
            fact_group="degree",
        ),
    ]

    record = reconcile("p1", PersonSeed(full_name="Jane Doe"), claims, sources, ScoringPolicy()).records[0]

    assert record.fields[ProfileField.profile_link].value == "https://university.example/jane"
