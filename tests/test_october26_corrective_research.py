"""Offline reconstructions, not captured claims; actual PDL labels are retained."""

from datetime import date

import pytest

from app.config import ScoringPolicy
from app.research.identity import assess_identity, eligible_identity_source_ids
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, IdentityMatch, PersonSeed, ProfileField, SourceRecord, SourceType

POLICY = ScoringPolicy()
TODAY = date(2026, 10, 26)
IDENTITY_CASES = [
    (
        "Achille Mbembe",
        "University of the Witwatersrand",
        "Philosopher and Research Professor",
        "University of the Witwatersrand",
        "Research Professor",
        SourceType.university,
    ),
    (
        "Elahe Mohammadi",
        "Ham-Mihan Newspaper",
        "Journalist (Society & Women's Issues Reporter)",
        "Ham-Mihan",
        "Journalist",
        SourceType.professional_body,
    ),
    (
        "Jonathan Anderson",
        "Christian Dior Couture; JW Anderson",
        "Creative Director; Founder",
        "Christian Dior Couture",
        "Creative Director",
        SourceType.employer,
    ),
    (
        "Zanele Muholi",
        "Inkanyiso",
        "Visual Activist and Photographer",
        "Inkanyiso",
        "Founder",
        SourceType.first_party,
    ),
    (
        "Gamini Wijesuriya",
        "ICCROM",
        "Project Manager / Special Advisor, World Heritage Leadership",
        "ICCROM",
        "Special Advisor",
        SourceType.professional_body,
    ),
]


def source(identifier, seed, text, kind=SourceType.employer, *, secure=False):
    url = f"https://{identifier}.example.org/biography"
    return SourceRecord(
        source_id=identifier,
        person_id="person",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain=f"{identifier}.example.org",
        content_hash=identifier,
        title=f"{seed.full_name} biography",
        source_type=kind,
        authority_score=POLICY.authority[kind],
        identity=IdentityMatch(score=1, ambiguous=False)
        if secure
        else assess_identity(seed, text, POLICY, model_relevance="likely", source_type=kind),
    )


def role(seed, page, organisation, title, quote, *, current=None, group="role"):
    result = []
    for field, value in [(ProfileField.organisation, organisation), (ProfileField.job_title, title)]:
        normalized, certainty = normalise(field, value)
        result.append(
            EvidenceClaim(
                claim_id=f"{page.source_id}-{group}-{field.value}",
                person_id="person",
                source_id=page.source_id,
                field=field,
                raw_value=value,
                normalised_value=normalized,
                normalisation_certainty=certainty,
                evidence_text=quote,
                subject_name=seed.full_name,
                identity_relevance=page.identity.score,
                extraction_model="offline-fixture",
                fact_group=group,
                is_current=current,
            )
        )
    return result


def identity_fixture(case):
    name, supplied_org, supplied_title, org, title, kind = case
    text = f"{name} is {title} at {org}."
    seed = PersonSeed(
        full_name=name, job_title=supplied_title, known_attributes={"alumni_organisation": supplied_org}
    )
    page = source("official", seed, text, kind)
    return seed, [page], role(seed, page, org, title, text, current=True)


@pytest.mark.parametrize("case", IDENTITY_CASES, ids=[case[0] for case in IDENTITY_CASES])
def test_pdl_affiliation_anchors_identity_without_exact_seed_job_title(case):
    seed, pages, claims = identity_fixture(case)
    assert eligible_identity_source_ids(seed, claims, {p.source_id: p for p in pages}, POLICY) == {"official"}
    profile = reconcile("person", seed, claims, pages, POLICY, TODAY)
    assert profile.fields[ProfileField.organisation].selected_claim_id == claims[0].claim_id
    assert profile.fields[ProfileField.job_title].selected_claim_id == claims[1].claim_id


def test_multiple_supplied_employers_anchor_without_cross_pairing_roles():
    seed, pages, claims = identity_fixture(IDENTITY_CASES[2])
    text = "Jonathan Anderson is Founder of JW Anderson."
    second = source("practice", seed, text)
    pages.append(second)
    claims.extend(role(seed, second, "JW Anderson", "Founder", text, current=True))
    assert eligible_identity_source_ids(seed, claims, {p.source_id: p for p in pages}, POLICY) == {
        "official",
        "practice",
    }
    fields = reconcile("person", seed, claims, pages, POLICY, TODAY).fields
    assert (
        fields[ProfileField.organisation].selected_claim_id.split("-role")[0]
        == fields[ProfileField.job_title].selected_claim_id.split("-role")[0]
    )


def test_namesake_cannot_use_generic_title_when_pdl_affiliation_is_supplied():
    seed, _, _ = identity_fixture(IDENTITY_CASES[0])
    text = "Achille Mbembe is Philosopher and Research Professor at Unrelated University."
    page = source("namesake", seed, text, SourceType.university)
    claims = role(
        seed, page, "Unrelated University", "Philosopher and Research Professor", text, current=True
    )
    assert eligible_identity_source_ids(seed, claims, {page.source_id: page}, POLICY) == set()


def test_seed_university_identifies_employment_without_supplied_employer():
    seed = PersonSeed(full_name="Ada Example", university_name="Example University")
    text = "Ada Example is Professor at Example University."
    page = source("faculty", seed, text, SourceType.university)
    claims = role(seed, page, "Example University", "Professor", text, current=True)
    assert eligible_identity_source_ids(seed, claims, {page.source_id: page}, POLICY) == {"faculty"}


PRACTICE_CASES = [
    (
        "Renzo Piano",
        "Renzo Piano Building Workshop",
        "Architect",
        None,
        "Renzo Piano established Renzo Piano Building Workshop as an Architect.",
        "His Father's Construction Company",
        "Architect",
        None,
        "Renzo Piano worked at His Father's Construction Company as an Architect.",
    ),
    (
        "Zaha Hadid",
        "Zaha Hadid Architects",
        "Founder",
        False,
        "Zaha Hadid was Founder of Zaha Hadid Architects until her death in 2016.",
        "Office for Metropolitan Architecture",
        "Architect",
        None,
        "Zaha Hadid previously worked as an Architect at Office for Metropolitan Architecture.",
    ),
    (
        "I. M. Pei",
        "Pei Cobb Freed & Partners",
        "Founding Partner",
        False,
        "I. M. Pei was Founding Partner at Pei Cobb Freed & Partners before he retired.",
        "Webb & Knapp",
        "Architect",
        None,
        "I. M. Pei worked as an Architect at Webb & Knapp.",
    ),
    (
        "Liu Jiakun",
        "Jiakun Architects",
        "Founder",
        None,
        "Liu Jiakun is Founder of Jiakun Architects.",
        "Central Academy of Fine Arts",
        "Visiting Professor",
        True,
        "Liu Jiakun is Visiting Professor at Central Academy of Fine Arts.",
    ),
    (
        "Jacques Herzog",
        "Herzog & de Meuron",
        "Architect and Founder",
        None,
        "Jacques Herzog is Architect and Founder of Herzog & de Meuron.",
        "ETH Zurich",
        "Professor",
        True,
        "Jacques Herzog is Professor at ETH Zurich.",
    ),
    (
        "Frank Gehry",
        "Gehry Partners",
        "Founder",
        None,
        "Frank Gehry is Founder of Gehry Partners.",
        "Yale University",
        "Charlotte Davenport Professorship in Architecture",
        True,
        "Frank Gehry holds the Charlotte Davenport Professorship in Architecture at Yale University.",
    ),
]


def practice_fixture(case):
    name, org, title, current, quote, alt_org, alt_title, alt_current, alt_quote = case
    seed = PersonSeed(full_name=name)
    primary = source("practice", seed, quote, secure=True)
    other = source("other", seed, alt_quote, secure=True)
    claims = [
        *role(seed, primary, org, title, quote, current=current),
        *role(seed, other, alt_org, alt_title, alt_quote, current=alt_current),
    ]
    return seed, [primary, other], claims


@pytest.mark.parametrize("case", PRACTICE_CASES, ids=[case[0] for case in PRACTICE_CASES])
def test_principal_practice_beats_early_employment_and_secondary_academic(case):
    seed, pages, claims = practice_fixture(case)
    profile = reconcile("person", seed, claims, pages, POLICY, TODAY)
    for field in (ProfileField.organisation, ProfileField.job_title):
        decision = profile.fields[field]
        assert decision.selected_claim_id == f"practice-role-{field.value}"
        assert decision.scoring_components["relationship_temporal_status"] == (
            "historical" if case[3] is False else "unspecified"
        )
    if case[3] is False:
        assert (
            "HISTORICAL_PROFESSIONAL_AFFILIATION"
            in profile.fields[ProfileField.organisation].review_reason_codes
        )
    assert claims[0].is_current is case[3]
    if case[0] == "Renzo Piano":
        title = profile.fields[ProfileField.job_title]
        assert title.supporting_claim_ids == ["practice-role-job_title"]
        assert "other-role-job_title" in title.alternative_claim_ids


def test_retired_founder_does_not_displace_new_current_principal_employer():
    seed = PersonSeed(full_name="Ada Example")
    old, new = source("former", seed, "", secure=True), source("current", seed, "", secure=True)
    claims = [
        *role(
            seed,
            old,
            "Example Studio",
            "Founder",
            "Ada Example retired as Founder of Example Studio.",
            current=False,
        ),
        *role(
            seed, new, "New Practice", "Architect", "Ada Example is Architect at New Practice.", current=True
        ),
    ]
    fields = reconcile("person", seed, claims, [old, new], POLICY, TODAY).fields
    assert fields[ProfileField.organisation].value == "New Practice"
    assert fields[ProfileField.organisation].scoring_components["relationship_temporal_status"] == "current"


def test_mixed_history_quote_and_family_named_institution_keep_current_role():
    seed = PersonSeed(full_name="Ada Example")
    page = source("official", seed, "", secure=True)
    quote = "Ada Example was Analyst at Old Group. Ada Example is Director at Family Health International."
    claims = role(seed, page, "Old Group", "Analyst", quote, current=True, group="old")
    claims.extend(
        role(seed, page, "Family Health International", "Director", quote, current=True, group="new")
    )
    fields = reconcile("person", seed, claims, [page], POLICY, TODAY).fields
    assert fields[ProfileField.organisation].value == "Family Health International"
    assert fields[ProfileField.job_title].value == "Director"


def test_unpaired_past_job_does_not_corroborate_the_same_current_title_at_another_employer():
    seed = PersonSeed(full_name="Ada Example")
    old, new = source("former", seed, "", secure=True), source("current", seed, "", secure=True)
    claims = [
        *role(
            seed,
            old,
            "Old Studio",
            "Architect",
            "Ada Example formerly worked as Architect at Old Studio.",
            current=False,
        )[1:],
        *role(
            seed,
            new,
            "New Studio",
            "Architect",
            "Ada Example is Architect at New Studio.",
            current=True,
        ),
    ]
    fields = reconcile("person", seed, claims, [old, new], POLICY, TODAY).fields
    assert fields[ProfileField.organisation].value == "New Studio"
    assert fields[ProfileField.job_title].supporting_claim_ids == ["current-role-job_title"]
