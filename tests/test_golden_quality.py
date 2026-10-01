"""Synthetic offline quality panel, not assertions about these people's live biographies.

Fixtures model the evidence patterns reported by the user. All decisions run through
production identity/normalization/reconciliation; names have no production special cases.
"""

from datetime import date

import pytest

from app.config import ScoringPolicy
from app.research.identity import assess_identity
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, IdentityMatch, PersonSeed, ProfileField, SourceRecord, SourceType

TODAY = date(2026, 9, 30)
POLICY = ScoringPolicy()


def page(key, seed, text, *, kind=SourceType.government, published=None, secure=False):
    url = f"https://{key}.example/profile"
    return SourceRecord(
        source_id=key,
        person_id="person",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain=f"{key}.example",
        title=f"{seed.full_name} — Official biography",
        source_type=kind,
        authority_score=POLICY.authority[kind],
        published_at=published,
        content_hash=key,
        identity=IdentityMatch(score=1, ambiguous=False) if secure else assess_identity(seed, text, POLICY),
    )


def fact(source, seed, field, value, *, group=None, current=None, observed=None, evidence=None):
    normalized, certainty = normalise(field, value)
    return EvidenceClaim(
        person_id="person",
        source_id=source.source_id,
        field=field,
        raw_value=value,
        normalised_value=normalized,
        normalisation_certainty=certainty,
        evidence_text=evidence or f"{seed.full_name}: {value}.",
        subject_name=seed.full_name,
        identity_relevance=source.identity.score,
        extraction_model="offline-fixture",
        fact_group=group,
        is_current=current,
        as_of_date=observed,
    )


def role(source, seed, organisation, title, *, group="role", current=True, observed=None):
    quote = f"{seed.full_name} is {title} at {organisation}."
    return [
        fact(source, seed, field, value, group=group, current=current, observed=observed, evidence=quote)
        for field, value in [(ProfileField.organisation, organisation), (ProfileField.job_title, title)]
    ]


def profile(seed, sources, claims):
    return reconcile("person", seed, claims, sources, POLICY, TODAY)


def test_golden_andrew_marsh_seed_context_excludes_football_namesakes():
    seed = PersonSeed(full_name="Andrew Marsh", organisation="Entergy")
    correct = page(
        "employer", seed, "Andrew Marsh is Chief Executive Officer at Entergy.", kind=SourceType.employer
    )
    athlete = page(
        "football", seed, "Andrew Marsh is Wide Receiver at Sports University.", kind=SourceType.university
    )
    news = page(
        "sports", seed, "Andrew Marsh is Wide Receiver at Sports University.", kind=SourceType.publication
    )
    claims = [*role(correct, seed, "Entergy", "Chief Executive Officer")]
    for source in [athlete, news]:
        claims += role(source, seed, "Sports University", "Wide Receiver")
        claims.append(
            fact(source, seed, ProfileField.university_name, "Sports University", group="education")
        )
    result = profile(seed, [correct, athlete, news], claims)
    assert result.fields[ProfileField.organisation].value == "Entergy"
    assert result.fields[ProfileField.job_title].value == "Chief Executive Officer"
    assert result.fields[ProfileField.university_name].value is None
    assert result.fields[ProfileField.full_name].sources == []
    assert profile(seed, [], []).fields[ProfileField.organisation].value is None


def test_golden_anutin_current_prime_minister_beats_old_analyst_and_party():
    seed = PersonSeed(full_name="Anutin Charnvirakul")
    official = page("government", seed, "", secure=True, published=date(2018, 1, 1))
    old = page("career", seed, "", kind=SourceType.publication, secure=True)
    party = page("party", seed, "", kind=SourceType.publication, secure=True)
    claims = [
        *role(official, seed, "Office of the Prime Minister", "Prime Minister"),
        *role(old, seed, "Technical Company", "Technical Analyst", current=None, observed=date(2024, 1, 1)),
        *role(party, seed, "Example Political Party", "Party Leader"),
    ]
    result = profile(seed, [official, old, party], claims)
    assert result.fields[ProfileField.job_title].value == "Prime Minister"
    assert result.fields[ProfileField.organisation].value == "Office of the Prime Minister"
    assert result.fields[ProfileField.job_title].confidence >= 75


@pytest.mark.parametrize("variant", ["Pm", "PM", "pm", "Prime minister", "PRIME MINISTER"])
def test_golden_jafar_hassan_current_pair_without_model_group_is_high_confidence(variant):
    seed = PersonSeed(full_name="Jafar Hassan", organisation="Prime Minister's Office")
    source = page(
        "official",
        seed,
        "Jafar Hassan is Prime Minister at Prime Minister's Office.",
        published=date(2016, 1, 1),
    )
    result = profile(seed, [source], role(source, seed, "Prime Minister's Office", variant, group=None))
    for field in [ProfileField.job_title, ProfileField.organisation]:
        assert result.fields[field].confidence >= 75
        assert not result.fields[field].review_required
    assert result.fields[ProfileField.job_title].value == "Prime Minister"
    assert result.fields[ProfileField.organisation].value == "Prime Minister's Office"
    assert result.fields[ProfileField.profile_link].value == source.final_url


def test_golden_tshering_tobgay_specific_supported_office_beats_generic_government():
    seed = PersonSeed(full_name="Tshering Tobgay")
    generic = page("generic", seed, "", secure=True)
    specific = page("office", seed, "", secure=True)
    generic.authority_score = 1
    claims = [
        *role(generic, seed, "Government", "Prime Minister"),
        *role(specific, seed, "Prime Minister's Office", "Prime Minister"),
    ]
    result = profile(seed, [generic, specific], claims)
    assert result.fields[ProfileField.organisation].value == "Prime Minister's Office"
    assert result.fields[ProfileField.job_title].value == "Prime Minister"
    assert not result.fields[ProfileField.job_title].review_required


def test_golden_petteri_orpo_primary_office_beats_secondary_administration():
    seed = PersonSeed(full_name="Petteri Orpo")
    source = page("office", seed, "", secure=True)
    claims = [
        *role(source, seed, "Prime Minister's Office", "Prime Minister", group="primary"),
        *role(
            source, seed, "Prime Minister's Office", "Head of the Prime Minister's Office", group="secondary"
        ),
    ]
    result = profile(seed, [source], claims)
    assert result.fields[ProfileField.job_title].value == "Prime Minister"
    assert not result.fields[ProfileField.job_title].review_required
    assert result.fields[ProfileField.job_title].alternative_claim_ids


@pytest.mark.parametrize(
    ("name", "organisation", "title"),
    [
        ("Mohamed Muizzu", "President's Office", "President"),
        ("Ali Asadov", "Cabinet of Ministers", "Prime Minister"),
    ],
)
def test_golden_current_official_biography_is_not_aged_by_page_creation(name, organisation, title):
    seed = PersonSeed(full_name=name, organisation=organisation)
    source = page("official", seed, f"{name} is {title} at {organisation}.", published=date(2018, 1, 1))
    claims = role(source, seed, organisation, title)
    claims += [
        fact(source, seed, ProfileField.university_name, "University X", group="education"),
        fact(source, seed, ProfileField.degree_type, "PhD", group="education"),
    ]
    result = profile(seed, [source], claims)
    assert result.fields[ProfileField.organisation].value == organisation
    assert result.fields[ProfileField.job_title].value == title
    for field in [ProfileField.organisation, ProfileField.job_title]:
        assert result.fields[field].confidence >= 75
        assert not result.fields[field].review_required
    assert result.fields[ProfileField.degree_type].value == "Doctoral Degree"
    assert result.fields[ProfileField.full_name].confidence == 100
    assert POLICY.review_threshold == 50


def test_golden_elizabeth_adams_unconnected_namesakes_never_form_one_profile():
    seed = PersonSeed(full_name="Elizabeth Adams")
    business = page(
        "business", seed, "Elizabeth Adams is CEO of Alpha Corporation.", kind=SourceType.employer
    )
    academic = page(
        "academic",
        seed,
        "Elizabeth Adams earned a PhD in History at Beta University.",
        kind=SourceType.university,
    )
    claims = [*role(business, seed, "Alpha Corporation", "CEO")]
    claims += [
        fact(academic, seed, field, value, group="education")
        for field, value in [
            (ProfileField.university_name, "Beta University"),
            (ProfileField.degree_type, "PhD"),
            (ProfileField.subject, "History"),
        ]
    ]
    result = profile(seed, [business, academic], claims)
    assert not (
        result.fields[ProfileField.organisation].value and result.fields[ProfileField.university_name].value
    )
    assert result.fields[ProfileField.full_name].value == "Elizabeth Adams"
    assert result.fields[ProfileField.full_name].confidence == 100


def test_golden_isaac_lungu_overlapping_subjects_do_not_manufacture_two_doctorates():
    seed = PersonSeed(full_name="Isaac Lungu")
    source = page("university", seed, "", kind=SourceType.university, secure=True)
    claims = []
    for group, degree, subject in [
        ("doctoral-a", "PhD", "Economics"),
        ("doctoral-b", "PhD", "Applied Economics"),
        ("masters", "MSc", "Economics"),
    ]:
        claims += [
            fact(source, seed, field, value, group=group)
            for field, value in [
                (ProfileField.university_name, "University X"),
                (ProfileField.degree_type, degree),
                (ProfileField.subject, subject),
            ]
        ]
    result = profile(seed, [source], claims)
    assert sorted(record.fields[ProfileField.degree_type].value for record in result.records) == [
        "Doctoral Degree",
        "Master's Degree",
    ]


def test_golden_hlabathe_posholi_credible_contributing_profile_link_survives():
    seed = PersonSeed(full_name="Hlabathe Posholi", organisation="Example Institute")
    source = page(
        "professional",
        seed,
        "Hlabathe Posholi is Director at Example Institute.",
        kind=SourceType.professional_body,
    )
    claims = [fact(source, seed, ProfileField.job_title, "Director")]
    result = profile(seed, [source], claims)
    assert result.fields[ProfileField.job_title].value == "Director"
    assert result.fields[ProfileField.profile_link].value == source.final_url
    assert result.fields[ProfileField.profile_link].supporting_source_ids == [source.source_id]
