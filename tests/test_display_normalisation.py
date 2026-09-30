"""Selected display values are deterministic; literal evidence stays unchanged."""

import pytest

from app.config import ScoringPolicy
from app.research.normalisation import normalise, normalise_display, normalise_value
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, IdentityMatch, PersonSeed, ProfileField, SourceRecord, SourceType


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("presidente", "President"),
        ("Presidente de la República", "President"),
        ("Président de la République", "President"),
        ("presidente da república", "President"),
        ("primer ministro", "Prime Minister"),
        ("premier ministre", "Prime Minister"),
        ("primeiro-ministro", "Prime Minister"),
        ("directeur général", "Director General"),
        ("director general", "Director General"),
        ("diretor-geral", "Director General"),
        ("secrétaire général", "Secretary General"),
        ("profesor universitario", "University Professor"),
    ],
)
def test_common_titles_use_deterministic_english(raw, expected):
    assert normalise_display(ProfileField.job_title, raw) == expected


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        (ProfileField.subject, "ciencias políticas", "Political Science"),
        (ProfileField.subject, "sciences politiques", "Political Science"),
        (ProfileField.subject, "ciência da computação", "Computer Science"),
        (ProfileField.subject, "administration publique", "Public Administration"),
        (ProfileField.subject, "matemáticas", "Mathematics"),
        (ProfileField.organisation, "Nations Unies", "United Nations"),
        (ProfileField.organisation, "ministério das finanças", "Ministry of Finance"),
        (ProfileField.degree_type, "licenciatura", "Bachelor's Degree"),
        (ProfileField.degree_type, "maîtrise", "Master's Degree"),
        (ProfileField.degree_type, "mestrado", "Master's Degree"),
        (ProfileField.degree_type, "doctorat", "Doctoral Degree"),
        (ProfileField.degree_type, "doutorado", "Doctoral Degree"),
        (ProfileField.degree_type, "PhD", "Doctoral Degree"),
        (ProfileField.degree_type, "MBA", "Master's Degree"),
    ],
)
def test_field_specific_english_equivalents(field, raw, expected):
    assert normalise_display(field, raw) == expected
    assert normalise_value(field, raw).certainty == 1


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        (ProfileField.full_name, "DONALD TRUMP", "Donald Trump"),
        (ProfileField.full_name, "donald trump", "Donald Trump"),
        (ProfileField.full_name, "Donald trump", "Donald Trump"),
        (ProfileField.full_name, "SEAN O'CONNOR", "Sean O'Connor"),
        (ProfileField.full_name, "sean o’connor", "Sean O’Connor"),
        (ProfileField.full_name, "anne-marie smith", "Anne-Marie Smith"),
        (ProfileField.full_name, "JAN VAN DER MEER", "Jan van der Meer"),
        (ProfileField.full_name, "de silva", "de Silva"),
        (ProfileField.full_name, "Ursula von der leyen", "Ursula von der Leyen"),
        (ProfileField.full_name, "john mcdonald", "John McDonald"),
        (ProfileField.full_name, "John MacDonald", "John MacDonald"),
        (ProfileField.full_name, "John McDonald", "John McDonald"),
        (ProfileField.full_name, "John J.D. Smith III", "John J.D. Smith III"),
        (ProfileField.job_title, "PRESIDENT", "President"),
        (ProfileField.job_title, "chief executive officer", "Chief Executive Officer"),
        (ProfileField.job_title, "CHIEF EXECUTIVE OFFICER", "Chief Executive Officer"),
        (ProfileField.job_title, "Chief executive officer", "Chief Executive Officer"),
        (ProfileField.job_title, "CEO", "CEO"),
        (ProfileField.job_title, "cfo", "CFO"),
        (ProfileField.job_title, "MBA", "MBA"),
        (ProfileField.job_title, "PhD", "PhD"),
        (ProfileField.job_title, "MD", "MD"),
        (ProfileField.job_title, "JD", "JD"),
        (ProfileField.job_title, "Senior Research Fellow", "Senior Research Fellow"),
        (ProfileField.job_title, "senior research fellow", "Senior Research Fellow"),
        (ProfileField.job_title, "Head of R&D", "Head of R&D"),
        (ProfileField.job_title, "SVP of Engineering", "SVP of Engineering"),
        (ProfileField.job_title, "CHIEF Executive Officer", "Chief Executive Officer"),
        (ProfileField.university_name, "university of staffordshire", "University of Staffordshire"),
        (ProfileField.university_name, "UNIVERSITY OF STAFFORDSHIRE", "University of Staffordshire"),
        (ProfileField.organisation, "ONGC", "ONGC"),
        (ProfileField.organisation, "ONGC ENERGY GROUP", "ONGC Energy Group"),
        (ProfileField.organisation, "UN office for Project Services", "UN Office for Project Services"),
        (ProfileField.organisation, "NATO", "NATO"),
        (ProfileField.organisation, "eBay", "eBay"),
        (ProfileField.organisation, "OpenAI", "OpenAI"),
        (ProfileField.organisation, "EBRD Foundation", "EBRD Foundation"),
        (ProfileField.organisation, "XYZ Foundation", "XYZ Foundation"),
        (ProfileField.university_name, "UCL School of Management", "UCL School of Management"),
        (ProfileField.subject, "computer science", "Computer Science"),
    ],
)
def test_display_casing_is_field_specific(field, raw, expected):
    assert normalise_display(field, raw) == expected


def test_unknown_proper_names_are_preserved_without_invented_english():
    for field in (ProfileField.organisation, ProfileField.university_name):
        assert normalise_display(field, "Universidade de São Paulo") == "Universidade de São Paulo"
    url = "https://example.org/MixedCase?Title=PRESIDENT#Profile"
    assert normalise_display(ProfileField.profile_link, url) == url


def test_cosmetic_casing_does_not_change_normalisation_certainty_or_evidence_keys():
    for field, value in (
        (ProfileField.full_name, "Donald Trump"),
        (ProfileField.organisation, "Example Company"),
        (ProfileField.job_title, "Chief Executive Officer"),
        (ProfileField.university_name, "University of Staffordshire"),
        (ProfileField.subject, "Computer Science"),
    ):
        assert normalise_value(field, value) == normalise_value(field, value.lower())
        assert normalise_value(field, value) == normalise_value(field, value.upper())


def test_reconciliation_normalises_presentation_without_rewriting_raw_evidence():
    seed = PersonSeed(full_name="JANE DOE")
    source = SourceRecord(
        source_id="s1",
        person_id="p1",
        requested_url="https://government.example/Jane",
        final_url="https://government.example/Jane",
        canonical_url="https://government.example/Jane",
        domain="government.example",
        title="Presidente de la República",
        source_type=SourceType.government,
        authority_score=0.95,
        identity=IdentityMatch(score=1, ambiguous=False),
    )
    claims = []
    for field, raw in (
        (ProfileField.organisation, "Presidency"),
        (ProfileField.job_title, "Presidente de la República"),
    ):
        normalized, certainty = normalise(field, raw)
        claims.append(
            EvidenceClaim(
                claim_id=field.value,
                person_id="p1",
                source_id="s1",
                field=field,
                raw_value=raw,
                normalised_value=normalized,
                normalisation_certainty=certainty,
                evidence_text=f"Jane Doe: {raw}",
                subject_name="Jane Doe",
                identity_relevance=1,
                extraction_model="fixture",
                fact_group="role",
                is_current=True,
            )
        )
    before = [claim.model_dump() for claim in claims]
    source_before = source.model_dump()

    profile = reconcile("p1", seed, claims, [source], ScoringPolicy())

    assert profile.fields[ProfileField.full_name].value == "Jane Doe"
    assert profile.fields[ProfileField.full_name].confidence == 100
    assert profile.fields[ProfileField.job_title].value == "President"
    assert profile.input_name == seed.full_name == "JANE DOE"
    assert [claim.model_dump() for claim in claims] == before
    assert source.model_dump() == source_before
