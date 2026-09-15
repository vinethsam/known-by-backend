from datetime import date

import pytest

from app.config import ScoringPolicy, Settings
from app.research.claims import validate_claims
from app.research.confidence import confidence_for_group, profile_scores
from app.research.identity import assess_identity
from app.research.normalisation import normalise
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


def source(identifier="s1", authority=0.9, domain="one.org", identity=1, content_hash=""):
    return SourceRecord(
        source_id=identifier,
        person_id="p1",
        requested_url=f"https://{domain}/jane",
        final_url=f"https://{domain}/jane",
        canonical_url=f"https://{domain}/jane",
        domain=domain,
        source_type=SourceType.employer,
        authority_score=authority,
        identity=IdentityMatch(score=identity, ambiguous=identity < 0.8),
        content_hash=content_hash,
    )


def claim(identifier="c1", source_id="s1", field=ProfileField.organisation, value="Example", **kwargs):
    return EvidenceClaim(
        claim_id=identifier,
        person_id="p1",
        source_id=source_id,
        field=field,
        raw_value=value,
        normalised_value=normalise(field, value)[0],
        evidence_text=f"Jane Doe works for {value}.",
        subject_name="Jane Doe",
        identity_relevance=1,
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
        ("BSc", "Bachelor's"),
        ("B.Sc.", "Bachelor's"),
        ("Bachelor of Science", "Bachelor's"),
        ("BSc (Hons)", "Bachelor's"),
        ("MSc", "Master's"),
        ("MA", "Master's"),
        ("MBA", "Master's"),
        ("MPH", "Master's"),
        ("PhD", "Doctorate"),
        ("DPhil", "Doctorate"),
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
    assert normalise(ProfileField.degree_type, "Basket weaving")[0] == "basket weaving"


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
        [source(), source("s2", authority=0.4, domain="two.org")],
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
    assert profile.fields[ProfileField.degree_type].value == "Bachelor's"
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
    assert profile.fields[ProfileField.degree_type].value == "Bachelor's"
    assert profile.fields[ProfileField.university_name].value is None
    assert "UNPAIRED_FACT" in profile.fields[ProfileField.university_name].review_reason_codes
