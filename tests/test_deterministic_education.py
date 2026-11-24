from collections import defaultdict
from datetime import date

import pytest

from app.config import ScoringPolicy
from app.research.claims import validate_claims
from app.research.deterministic.context import EvidenceSpan
from app.research.deterministic.education import extract_education
from app.research.reconciliation import reconcile
from app.schemas import ExtractionResponse, IdentityMatch, PersonSeed, SourceRecord

SEED = PersonSeed(full_name="Amara Fennick")
FIELDS = ("university_name", "degree_type", "subject")


def extract(body, section=""):
    quote = f"{SEED.full_name} {body}"
    span = EvidenceSpan(quote, body, SEED.full_name, "chunk:0-99", section, "fixture:0-99")
    claims = extract_education(span, SEED)
    assert all(
        claim.value in quote and claim.evidence == quote and claim.evidence_location == span.location
        for claim in claims
    )
    return claims


def rows(claims):
    grouped = defaultdict(dict)
    for claim in claims:
        grouped[claim.fact_group][claim.field] = claim.value
    return [tuple(fields.get(field) for field in FIELDS) for fields in grouped.values()]


@pytest.mark.parametrize(
    ("body", "section", "expected"),
    [
        (
            "earned a BSc in Chemistry from Eastmere University.",
            "",
            ("Eastmere University", "BSc", "Chemistry"),
        ),
        (
            "graduated from the University of Eastmere with an MSc in Geology.",
            "",
            ("University of Eastmere", "MSc", "Geology"),
        ),
        (
            "completed a Master of Chemical Engineering from MIT.",
            "",
            ("MIT", "Master of Chemical Engineering", "Chemical Engineering"),
        ),
        (
            "obtained an M.B.A. with a major in Finance at Eastmere University.",
            "",
            ("Eastmere University", "M.B.A.", "Finance"),
        ),
        ("studied at Reedhaven University.", "", ("Reedhaven University", None, None)),
        ("attended Reedhaven University.", "", ("Reedhaven University", None, None)),
        (
            "earned a degree in Chemistry from Eastmere University.",
            "",
            ("Eastmere University", None, "Chemistry"),
        ),
        ("has a PhD in Chemistry from Eastmere University.", "", ("Eastmere University", "PhD", "Chemistry")),
        (
            "earned a Bachelor of Science (Honours) in Chemistry from Eastmere University.",
            "",
            ("Eastmere University", "Bachelor of Science", "Chemistry"),
        ),
        ("majored in Geology at Reedhaven University.", "", ("Reedhaven University", None, "Geology")),
        (
            "| BSc | Chemistry | Eastmere University |",
            "Education",
            ("Eastmere University", "BSc", "Chemistry"),
        ),
        (
            "**MSc** in Geology, [Eastmere University](https://eastmere.example.edu/)",
            "Education",
            ("Eastmere University", "MSc", "Geology"),
        ),
        (
            "Education: MSc in Geology at Eastmere University",
            "Amara Fennick",
            ("Eastmere University", "MSc", "Geology"),
        ),
    ],
)
def test_education_forms_preserve_exact_evidence_and_relationship(body, section, expected):
    assert rows(extract(body, section)) == [expected]


def test_shared_suffix_requires_explicit_shared_wording():
    assert rows(extract("earned both a BSc and an MSc in Chemistry from Eastmere University.")) == [
        ("Eastmere University", "BSc", "Chemistry"),
        ("Eastmere University", "MSc", "Chemistry"),
    ]
    assert rows(extract("earned a BSc and an MSc in Chemistry from Eastmere University.")) == [
        (None, "BSc", None),
        ("Eastmere University", "MSc", "Chemistry"),
    ]


def test_distinct_qualifications_survive_grounding_and_selection():
    claims = extract(
        "earned an MSc in Geology from Eastmere University in 2015 and "
        "an MSc in Data Science from Eastmere University in 2020 before "
        "earning a PhD in Mathematics from Northfen University."
    )
    assert len({claim.fact_group for claim in claims}) == 3
    assert {claim.temporal_context for claim in claims} == {None, "2015", "2020"}
    url = "https://eastmere.example.edu/amara"
    source = SourceRecord(
        source_id="source",
        person_id="person",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain="eastmere.example.edu",
        source_type="university",
        authority_score=1,
        identity=IdentityMatch(score=1, ambiguous=False),
    )
    evidence = claims[0].evidence
    grounded, rejected = validate_claims(
        ExtractionResponse(claims=claims), SEED, source, evidence, "deterministic:test"
    )
    assert len(grounded) == 9
    assert not rejected
    profile = reconcile("person", SEED, grounded, [source], ScoringPolicy(), today=date(2026, 11, 10))
    assert {tuple(record.fields[field].value for field in FIELDS) for record in profile.records} == {
        ("Eastmere University", "Master's Degree", "Geology"),
        ("Eastmere University", "Master's Degree", "Data Science"),
        ("Northfen University", "Doctoral Degree", "Mathematics"),
    }


@pytest.mark.parametrize(
    "body",
    [
        "teaches Chemistry at Eastmere University.",
        "received an honorary PhD from Eastmere University.",
        "pursues a PhD in Biology at Eastmere University.",
        "received an MSc scholarship at Eastmere University.",
        "holds a master class in ceramic glazing.",
        "attended a lecture on Economics at Eastmere University.",
        "attended the Eastmere University Research Conference.",
        "studied at NASA.",
    ],
)
def test_unrelated_and_non_earned_references_do_not_create_academic_claims(body):
    assert not extract(body)


def test_ambiguous_institutions_do_not_attach_to_degree():
    assert rows(extract("earned a BSc in Chemistry from Eastmere University or Northfen University.")) == [
        (None, "BSc", "Chemistry")
    ]
