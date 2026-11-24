from datetime import date

import pytest

from app.research.claims import validate_claims
from app.research.deterministic.context import EvidenceSpan, evidence_spans
from app.research.deterministic.employment import extract_employment
from app.schemas import ExtractionResponse, IdentityMatch, PersonSeed, ProfileField, SourceRecord, SourceType

SEED = PersonSeed(
    full_name="Cora Isbourne", organisation="Lakehaven Transit", job_title="Operations Director"
)
URL = "https://lakehaven.example.com/leadership/cora-isbourne"
SOURCE = SourceRecord(
    person_id="cora",
    requested_url=URL,
    final_url=URL,
    canonical_url=URL,
    domain="lakehaven.example.com",
    source_type=SourceType.employer,
    identity=IdentityMatch(score=0.95, ambiguous=False),
)


def proposed(body, *, section="", text=None):
    span = EvidenceSpan(text or f"{SEED.full_name} {body}", body, SEED.full_name, "chunk:0", section, "test")
    return extract_employment(span, SEED)


@pytest.mark.parametrize(
    ("statement", "employer", "current"),
    [
        ("works for Lakehaven Transit.", "Lakehaven Transit", True),
        ("formerly worked at Bayhaven Motors.", "Bayhaven Motors", False),
        ("is currently employed by IBM.", "IBM", True),
        ("joined International Business Machines (IBM).", "International Business Machines", None),
    ],
)
def test_employer_only_uses_literal_work_relationship(statement, employer, current):
    (claim,) = proposed(statement)
    assert (claim.field, claim.value, claim.is_current) == (ProfileField.organisation, employer, current)
    assert employer in claim.evidence


@pytest.mark.parametrize(
    ("statement", "title"),
    [
        ("is a Research Director.", "Research Director"),
        ("is working as an architect.", "architect"),
        ("serves as Chief Executive Officer (CEO).", "Chief Executive Officer"),
        ("is Professor of Computer Science.", "Professor of Computer Science"),
        ("is Director of Research and Development.", "Director of Research and Development"),
    ],
)
def test_title_only_preserves_the_role_without_copying_seed_employer(statement, title):
    (claim,) = proposed(statement)
    assert (claim.field, claim.value) == (ProfileField.job_title, title)


def test_current_and_historical_pairs_are_grounded_as_separate_facts():
    text = (
        "As of 2026-03-04, Cora Isbourne currently serves as Operations Director at Lakehaven Transit. "
        "Cora Isbourne formerly worked as Fleet Manager at Bayhaven Motors until 31 July 2023."
    )
    proposals = [
        claim for span in evidence_spans(text, SEED, SOURCE) for claim in extract_employment(span, SEED)
    ]
    accepted, reasons = validate_claims(ExtractionResponse(claims=proposals), SEED, SOURCE, text, "test")
    assert not reasons
    active, former = accepted[:2], accepted[2:]
    assert [claim.raw_value for claim in active] == ["Lakehaven Transit", "Operations Director"]
    assert [claim.raw_value for claim in former] == ["Bayhaven Motors", "Fleet Manager"]
    assert len({claim.fact_group for claim in active}) == len({claim.fact_group for claim in former}) == 1
    assert active[0].fact_group != former[0].fact_group
    assert all(claim.is_current is True and claim.as_of_date == date(2026, 3, 4) for claim in active)
    assert all(claim.is_current is False and claim.end_date == date(2023, 7, 31) for claim in former)


def test_coordinated_work_verbs_bind_each_job_title_to_its_own_employer():
    result = proposed(
        "serves as Operations Director at Lakehaven Transit and previously worked for Bayhaven Motors "
        "as Fleet Manager until 2023."
    )
    assert [(claim.value, claim.is_current) for claim in result] == [
        ("Lakehaven Transit", True),
        ("Operations Director", True),
        ("Bayhaven Motors", False),
        ("Fleet Manager", False),
    ]
    assert result[0].fact_group == result[1].fact_group != result[2].fact_group == result[3].fact_group
    assert result[2].end_date is None and result[2].temporal_context == "until 2023"


def test_owned_career_table_preserves_link_label_and_literal_range():
    row = "| Fleet Manager | [Bayhaven Motors](https://bayhaven.example.com/) | 2019–2023 |"
    text = f"# {SEED.full_name}\n\n## Professional Experience\n\n{row}"
    result = proposed(row, section="Professional Experience", text=text)
    assert [claim.value for claim in result] == ["Bayhaven Motors", "Fleet Manager"]
    assert all(claim.evidence == text and claim.is_current is False for claim in result)
    assert all(claim.temporal_context == "2019–2023" and claim.end_date is None for claim in result)
    assert not proposed(row, section="Education", text=text)


def test_owned_position_label_retains_internal_title_qualifier():
    result = proposed("Current position: Vice President for Research at Lakehaven Transit.")
    assert [claim.value for claim in result] == ["Lakehaven Transit", "Vice President for Research"]
    assert all(claim.is_current is True for claim in result)


@pytest.mark.parametrize(
    ("statement", "current"),
    [
        (", Operations Director at Lakehaven Transit, addressed the meeting.", None),
        (", the former Operations Director of Lakehaven Transit, addressed the meeting.", False),
        (", the current Operations Director at Lakehaven Transit, addressed the meeting.", True),
    ],
)
def test_apposition_with_an_explicit_role_and_employer(statement, current):
    result = proposed(statement)
    assert [claim.value for claim in result] == ["Lakehaven Transit", "Operations Director"]
    assert all(claim.is_current is current for claim in result)


def test_owned_apposition_can_be_grounded_without_taking_companion_role():
    text = (
        "Cora Isbourne, Operations Director at Lakehaven Transit, addressed the meeting. "
        "Cora Isbourne, accompanied by the Director at Norford Capital, answered questions."
    )
    proposals = [
        claim for span in evidence_spans(text, SEED, SOURCE) for claim in extract_employment(span, SEED)
    ]
    accepted, reasons = validate_claims(ExtractionResponse(claims=proposals), SEED, SOURCE, text, "test")
    assert not reasons
    assert [claim.raw_value for claim in accepted] == ["Lakehaven Transit", "Operations Director"]


@pytest.mark.parametrize(
    "statement",
    [
        "thanked Jasper Fenwick, the Chief Executive Officer at Norford Capital.",
        "works with Norford Capital on a project.",
        "is a friend of the Director at Norford Capital.",
        "is a candidate for Chief Executive Officer at Norford Capital.",
        "was not the Director at Norford Capital.",
        "was interviewed by the Director at Norford Capital.",
        ", accompanied by the Director at Norford Capital, addressed the meeting.",
        ", meeting with the Director of Norford Capital, arrived on time.",
        "studied Engineering at Norford University.",
        "works for a company.",
        "works for Bayhaven Motors and Lakehaven Transit.",
        "lives in London.",
    ],
)
def test_unrelated_entities_and_non_actual_roles_do_not_become_claims(statement):
    assert not proposed(statement)


def test_adjacent_namesake_does_not_contribute_an_employer_or_title():
    text = (
        "# Cora Isbourne\n\nCora Isbourne works as Operations Director at Lakehaven Transit.\n\n"
        "## Cora Isbourne-Watt\n\nCora Isbourne-Watt is Chief Executive Officer at Norford Capital."
    )
    claims = [
        claim for span in evidence_spans(text, SEED, SOURCE) for claim in extract_employment(span, SEED)
    ]
    assert [claim.value for claim in claims] == ["Lakehaven Transit", "Operations Director"]
