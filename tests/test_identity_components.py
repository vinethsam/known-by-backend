"""Name-only research must keep unrelated people's evidence separate."""

from itertools import permutations

import pytest

from app.config import ScoringPolicy
from app.research.identity import assess_identity, eligible_identity_source_ids
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, PersonSeed, ProfileField, SourceRecord, SourceType

POLICY = ScoringPolicy()
SEED = PersonSeed(full_name="Elizabeth Adams")


def source(identifier, *, secure=False, authority=0.9):
    match = assess_identity(SEED, "Elizabeth Adams has a professional biography.", POLICY)
    if secure:
        match = match.model_copy(update={"score": 0.9, "ambiguous": False})
    url = f"https://{identifier}.example/profile"
    return SourceRecord(
        source_id=identifier,
        person_id="p",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain=f"{identifier}.example",
        source_type=SourceType.employer,
        authority_score=authority,
        identity=match,
        content_hash=identifier,
    )


def claim(item, field, value, **updates):
    normalised, certainty = normalise(field, value)
    data = dict(
        person_id="p",
        source_id=item.source_id,
        field=field,
        raw_value=value,
        normalised_value=normalised,
        normalisation_certainty=certainty,
        evidence_text=f"Elizabeth Adams: {value}",
        subject_name=SEED.full_name,
        identity_relevance=item.identity.score,
        extraction_model="offline-fixture",
        fact_group="role" if field in {ProfileField.organisation, ProfileField.job_title} else "education",
        is_current=True if field in {ProfileField.organisation, ProfileField.job_title} else None,
    )
    data.update(updates)
    return EvidenceClaim(**data)


def eligible(claims, *items):
    return eligible_identity_source_ids(SEED, claims, {item.source_id: item for item in items}, POLICY)


def test_disconnected_namesakes_cannot_combine_role_and_education_in_final_profile():
    business, academic = source("business"), source("academic")
    claims = [
        claim(business, ProfileField.organisation, "Alpha Corporation"),
        claim(business, ProfileField.job_title, "Chief Executive Officer"),
        claim(academic, ProfileField.university_name, "Beta University"),
        claim(academic, ProfileField.degree_type, "PhD"),
        claim(academic, ProfileField.subject, "History"),
    ]
    before = [item.model_dump() for item in claims]
    profile = reconcile("p", SEED, claims, [business, academic], POLICY)

    assert profile.fields[ProfileField.full_name].value == SEED.full_name
    assert profile.fields[ProfileField.full_name].confidence == 100
    assert all(
        decision.value is None
        for field, decision in profile.fields.items()
        if field != ProfileField.full_name
    )
    assert before == [item.model_dump() for item in claims]


def test_single_name_only_source_remains_useful_and_reviewable():
    item = source("one")
    claims = [
        claim(item, ProfileField.organisation, "Alpha Corporation"),
        claim(item, ProfileField.job_title, "Chief Executive Officer"),
    ]
    profile = reconcile("p", SEED, claims, [item], POLICY)
    assert profile.fields[ProfileField.organisation].value == "Alpha Corporation"
    assert profile.review_required
    assert eligible(claims, item) == {"one"}


@pytest.mark.parametrize("field", [ProfileField.organisation, ProfileField.university_name])
def test_shared_explicit_institution_connects_name_only_sources(field):
    first, second = source("first"), source("second")
    claims = [claim(item, field, "Example Institute") for item in (first, second)]
    assert eligible(claims, first, second) == {"first", "second"}


def test_institution_can_connect_employment_to_education_context():
    employer, education = source("employer"), source("education")
    claims = [
        claim(employer, ProfileField.organisation, "Example University"),
        claim(education, ProfileField.university_name, "Example University"),
    ]
    assert eligible(claims, employer, education) == {"employer", "education"}


@pytest.mark.parametrize("value", ["Government", "The Government", "University"])
def test_generic_institution_does_not_join_namesakes(value):
    first, second = source("first"), source("second")
    claims = [claim(item, ProfileField.organisation, value) for item in (first, second)]
    assert eligible(claims, first, second) == set()


def test_name_degree_and_common_role_subject_do_not_join_namesakes():
    first, second = source("first"), source("second")
    claims = [
        claim(item, field, value)
        for item in (first, second)
        for field, value in (
            (ProfileField.full_name, SEED.full_name),
            (ProfileField.degree_type, "PhD"),
            (ProfileField.job_title, "Professor"),
            (ProfileField.subject, "Economics"),
        )
    ]
    assert eligible(claims, first, second) == set()


def test_secure_sources_remain_eligible_without_cross_source_corroboration():
    employer, education = source("employer", secure=True), source("education", secure=True)
    claims = [
        claim(employer, ProfileField.organisation, "Alpha Corporation"),
        claim(education, ProfileField.university_name, "Beta University"),
    ]
    assert eligible(claims, employer, education) == {"employer", "education"}


def test_ambiguous_source_must_connect_directly_to_secure_source():
    secure, bridge, disconnected = source("secure", secure=True), source("bridge"), source("third")
    claims = [
        claim(secure, ProfileField.organisation, "Alpha Corporation"),
        claim(bridge, ProfileField.organisation, "Alpha Corporation"),
        claim(bridge, ProfileField.university_name, "Beta University"),
        claim(disconnected, ProfileField.university_name, "Beta University"),
    ]
    assert eligible(claims, secure, bridge, disconnected) == {"secure", "bridge"}


@pytest.mark.parametrize(
    "updates",
    [
        {"directness": "implied"},
        {"identity_relevance": 0.1},
        {"normalisation_certainty": 0.4},
    ],
)
def test_weak_institution_claim_cannot_create_identity_bridge(updates):
    secure, ambiguous = source("secure", secure=True), source("ambiguous")
    claims = [
        claim(secure, ProfileField.organisation, "Alpha Corporation"),
        claim(ambiguous, ProfileField.organisation, "Alpha Corporation", **updates),
        claim(ambiguous, ProfileField.job_title, "Director"),
    ]
    assert eligible(claims, secure, ambiguous) == {"secure"}


def test_unconnected_weak_source_does_not_veto_a_single_credible_biography():
    credible, weak = source("credible"), source("weak", authority=0.25)
    claims = [
        claim(credible, ProfileField.organisation, "Alpha Corporation"),
        claim(weak, ProfileField.university_name, "Beta University"),
    ]
    assert eligible(claims, credible, weak) == {"credible"}


def test_pages_without_substantive_claims_do_not_veto_a_usable_source():
    useful, name_only, failed = source("useful"), source("name"), source("failed")
    claims = [
        claim(useful, ProfileField.organisation, "Alpha Corporation"),
        claim(name_only, ProfileField.full_name, SEED.full_name),
    ]
    assert eligible(claims, useful, name_only, failed) == {"useful", "name", "failed"}


def test_largest_unanchored_component_is_not_assumed_to_be_intended_person():
    first, corroborating, other = source("first"), source("corroborating"), source("other")
    claims = [
        claim(first, ProfileField.organisation, "Alpha Corporation"),
        claim(corroborating, ProfileField.organisation, "Alpha Corporation"),
        claim(other, ProfileField.university_name, "Beta University"),
    ]
    for ordering in permutations((first, corroborating, other)):
        assert eligible(claims, *ordering) == set()
        assert eligible(list(reversed(claims)), *ordering) == set()
