"""Offline relationship patterns, not archived biographies of the named people."""

from datetime import date

import pytest

from app.config import ScoringPolicy
from app.research.normalisation import RelationshipType, classify_relationship, normalise
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, IdentityMatch, PersonSeed, ProfileField, SourceRecord, SourceType

TODAY = date(2026, 10, 22)
POLICY = ScoringPolicy()


def _source(
    source_id: str, *, published: date | None = None, kind: SourceType = SourceType.employer
) -> SourceRecord:
    domain = f"{source_id}.example"
    url = f"https://{domain}/profile"
    return SourceRecord(
        source_id=source_id,
        person_id="person",
        requested_url=url,
        final_url=url,
        canonical_url=url,
        domain=domain,
        source_type=kind,
        authority_score=POLICY.authority[kind],
        published_at=published,
        content_hash=source_id,
        identity=IdentityMatch(score=1),
    )


def _role(
    seed: PersonSeed,
    source: SourceRecord,
    organisation: str,
    title: str,
    *,
    current: bool | None,
    observed: date | None,
    group: str = "role",
) -> list[EvidenceClaim]:
    quote = f"{seed.full_name} is {title} at {organisation}."
    result = []
    for field_name, value in (
        (ProfileField.organisation, organisation),
        (ProfileField.job_title, title),
    ):
        normalized, certainty = normalise(field_name, value)
        result.append(
            EvidenceClaim(
                claim_id=f"{source.source_id}-{group}-{field_name.value}",
                person_id="person",
                source_id=source.source_id,
                field=field_name,
                raw_value=value,
                normalised_value=normalized,
                normalisation_certainty=certainty,
                evidence_text=quote,
                subject_name=seed.full_name,
                identity_relevance=1,
                extraction_model="offline-fixture",
                fact_group=group,
                is_current=current,
                as_of_date=observed,
            )
        )
    return result


def _profile(seed: PersonSeed, sources: list[SourceRecord], claims: list[EvidenceClaim]):
    return reconcile("person", seed, claims, sources, POLICY, TODAY).records[0]


def test_newer_primary_employer_beats_stale_explicit_executive_label():
    # Ajei Gopal-style pattern: an old page still labels its role "current", but
    # a recent employer page supports a different organisation/title pair.
    seed = PersonSeed(full_name="Ajei Gopal")
    old, recent = _source("older-employer"), _source("recent-employer")
    claims = [
        *_role(seed, old, "Legacy Systems", "Chief Executive Officer", current=True, observed=date(2018, 1, 1)),
        *_role(seed, recent, "New Systems", "Head of Operations", current=None, observed=date(2026, 9, 1)),
    ]

    record = _profile(seed, [old, recent], claims)

    assert record.fields[ProfileField.organisation].value == "New Systems"
    assert record.fields[ProfileField.job_title].value == "Head of Operations"
    assert record.fields[ProfileField.organisation].selected_claim_id == "recent-employer-role-organisation"
    assert record.fields[ProfileField.job_title].selected_claim_id == "recent-employer-role-job_title"
    assert "older-employer-role-organisation" in record.fields[ProfileField.organisation].alternative_claim_ids
    assert "older-employer-role-job_title" in record.fields[ProfileField.job_title].alternative_claim_ids


def test_new_publication_about_former_ceo_cannot_displace_explicit_current_employer():
    seed = PersonSeed(full_name="Jane Doe")
    current = _source("current-employer", published=date(2026, 8, 1))
    article = _source("new-publication", published=date(2026, 9, 1), kind=SourceType.publication)
    old_role = _role(seed, article, "Former Company", "Chief Executive Officer", current=None, observed=None)
    for claim in old_role:
        claim.evidence_text = "Jane Doe was Chief Executive Officer at Former Company."
    claims = [
        *_role(seed, current, "Current Company", "Head of Operations", current=True, observed=date(2026, 8, 1)),
        *old_role,
    ]

    record = _profile(seed, [current, article], claims)

    assert record.fields[ProfileField.organisation].value == "Current Company"
    assert record.fields[ProfileField.job_title].value == "Head of Operations"
    assert "new-publication-role-organisation" in record.fields[ProfileField.organisation].alternative_claim_ids


@pytest.mark.parametrize(
    ("name", "primary_title", "board_title"),
    [
        ("Benoît Bazin", "Chairman and CEO", "Administrateur"),
        ("Aziz Fakhroo", "Group CEO", "Deputy President Commissioner"),
    ],
)
def test_current_primary_employment_beats_newer_secondary_board_role(name, primary_title, board_title):
    seed = PersonSeed(full_name=name)
    primary, board = _source("primary-employer"), _source("board-affiliation")
    claims = [
        *_role(seed, primary, "Example Group", primary_title, current=True, observed=date(2026, 8, 1)),
        *_role(seed, board, "Example Trust", board_title, current=True, observed=date(2026, 9, 1)),
    ]

    record = _profile(seed, [primary, board], claims)

    assert record.fields[ProfileField.organisation].value == "Example Group"
    assert record.fields[ProfileField.job_title].value == primary_title
    assert classify_relationship("Example Trust", board_title) is RelationshipType.board_role
    assert record.fields[ProfileField.organisation].selected_claim_id == "primary-employer-role-organisation"
    assert record.fields[ProfileField.job_title].selected_claim_id == "primary-employer-role-job_title"
    assert "board-affiliation-role-organisation" in record.fields[ProfileField.organisation].alternative_claim_ids
    assert "board-affiliation-role-job_title" in record.fields[ProfileField.job_title].alternative_claim_ids


def test_explicitly_historical_role_remains_an_alternative():
    seed = PersonSeed(full_name="Andrew Horton")
    old, recent = _source("historical-employer"), _source("current-employer")
    claims = [
        *_role(seed, old, "Former Company", "Chief Executive Officer", current=False, observed=date(2018, 1, 1)),
        *_role(seed, recent, "Current Company", "Managing Director", current=True, observed=date(2026, 9, 1)),
    ]

    record = _profile(seed, [old, recent], claims)

    assert record.fields[ProfileField.organisation].value == "Current Company"
    assert record.fields[ProfileField.job_title].value == "Managing Director"
    assert "historical-employer-role-organisation" in record.fields[ProfileField.organisation].alternative_claim_ids
    assert "historical-employer-role-job_title" in record.fields[ProfileField.job_title].alternative_claim_ids
