from datetime import date

import pytest

from app.config import ScoringPolicy, Settings
from app.export.batch import parse_batch
from app.research.discovery import build_query, candidate_score
from app.research.identity import assess_identity, research_seed_payload, seed_identity_anchors
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, PersonSeed, ProfileField, SourceCandidate, SourceRecord, SourceType


def settings(**overrides) -> Settings:
    values = {"APP_ENV": "test", "MAX_INPUT_COLUMNS": 12}
    values.update(overrides)
    return Settings(**values)


def source(identifier: str, seed: PersonSeed, text: str) -> SourceRecord:
    identity = assess_identity(seed, text, ScoringPolicy(), model_relevance="likely")
    return SourceRecord(
        source_id=identifier,
        person_id="person",
        requested_url=f"https://official.example/{identifier}",
        final_url=f"https://official.example/{identifier}",
        canonical_url=f"https://official.example/{identifier}",
        domain="official.example",
        source_type=SourceType.employer,
        authority_score=0.9,
        identity=identity,
        content_hash=identifier,
    )


def claim(identifier: str, source_id: str, value: str, **updates) -> EvidenceClaim:
    normalised_value, certainty = normalise(ProfileField.organisation, value)
    return EvidenceClaim(
        claim_id=identifier,
        person_id="person",
        source_id=source_id,
        field=ProfileField.organisation,
        raw_value=value,
        normalised_value=normalised_value,
        normalisation_certainty=certainty,
        evidence_text=f"Jane Doe works for {value}.",
        subject_name="Jane Doe",
        identity_relevance=updates.pop("identity_relevance"),
        extraction_model="fixture",
        **updates,
    )


def test_name_only_seed_keeps_the_initial_query_bounded_to_the_name():
    seed = PersonSeed(full_name="Jane Doe")

    assert build_query(seed) == '"Jane Doe"'
    assert seed_identity_anchors(seed) == {}


def test_batch_headers_preserve_current_alumni_historical_and_government_semantics():
    batch = parse_batch(
        (
            "name,current_employer,organisation,alumni_organisation,former_employer,government_office\n"
            "Jane Doe,Current Company,Industry Association,Alumni Network,Former Company,Ministry of Science\n"
        ).encode(),
        "people.csv",
        None,
        settings(),
    )

    seed = batch.seeds[0]
    assert seed.organisation == "Current Company"
    assert seed.known_attributes == {
        "current_employer": "Current Company",
        "government_office": "Ministry of Science",
        "alumni_organisation": "Alumni Network",
        "historical_employer": "Former Company",
        "organisation_affiliation": "Industry Association",
    }
    query = build_query(seed)
    assert all(
        value in query
        for value in (
            "Current Company",
            "Industry Association",
            "Alumni Network",
            "Former Company",
            "Ministry of Science",
        )
    )


def test_current_employer_remains_the_stronger_anchor_than_generic_affiliation():
    seed = parse_batch(
        b"name,current_employer,organisation\nJane Doe,Current Company,Industry Association\n",
        "people.csv",
        None,
        settings(),
    ).seeds[0]

    assert seed_identity_anchors(seed) == {"organisation": "Current Company"}
    supporting = assess_identity(
        seed,
        "Jane Doe is a member of Industry Association.",
        ScoringPolicy(),
    )
    verified = assess_identity(
        seed,
        "Jane Doe works for Current Company and belongs to Industry Association.",
        ScoringPolicy(),
    )

    assert supporting.signals["matched_seed_anchors"] == []
    assert "SEED_IDENTITY_UNCONFIRMED" in supporting.reason_codes
    assert verified.signals["matched_seed_anchors"] == ["organisation"]
    assert verified.score > supporting.score


@pytest.mark.parametrize(
    ("header", "attribute", "typed_organisation"),
    [
        ("current_employer_name", "current_employer", "Example Institution"),
        ("government_ministry_name", "government_office", "Example Institution"),
        ("ministry", "government_office", "Example Institution"),
        ("alumni_organization_name", "alumni_organisation", None),
        ("former_organisation_name", "historical_employer", None),
    ],
)
def test_affiliation_header_variants_keep_their_semantics(header, attribute, typed_organisation):
    seed = parse_batch(
        f"name,{header}\nJane Doe,Example Institution\n".encode(),
        "people.csv",
        None,
        settings(),
    ).seeds[0]

    assert seed.organisation == typed_organisation
    assert seed.known_attributes == {attribute: "Example Institution"}


def test_same_name_candidate_matching_seed_organisation_is_strongly_preferred():
    seed = PersonSeed(full_name="Andrew Marsh", organisation="Entergy")
    executive = SourceCandidate(
        url="https://energy.example/andrew-marsh",
        title="Andrew Marsh | Entergy",
        snippet="Andrew Marsh is an executive at Entergy.",
        source_type=SourceType.employer,
        relevance="likely",
    )
    namesake = executive.model_copy(
        update={
            "url": "https://sports.example/andrew-marsh",
            "title": "Andrew Marsh | Football",
            "snippet": "Andrew Marsh is a college athlete.",
        }
    )

    assert candidate_score(executive, seed, settings()) > candidate_score(namesake, seed, settings())


def test_web_verification_of_seed_organisation_strengthens_identity():
    text = "Andrew Marsh is Chief Executive Officer of Entergy."
    policy = ScoringPolicy()
    name_only = assess_identity(PersonSeed(full_name="Andrew Marsh"), text, policy)
    verified = assess_identity(
        PersonSeed(full_name="Andrew Marsh", organisation="Entergy"),
        text,
        policy,
    )

    assert verified.score > name_only.score
    assert verified.signals["matched_seed_anchors"] == ["organisation"]


def test_newer_verified_employer_wins_without_turning_seed_context_into_evidence():
    seed = PersonSeed(full_name="Jane Doe", organisation="Organisation A")
    text = "Jane Doe previously worked for Organisation A. Jane Doe now works for Organisation B."
    official = source("official", seed, text)
    claims = [
        claim(
            "historical",
            official.source_id,
            "Organisation A",
            identity_relevance=official.identity.score,
            fact_group="historical-role",
            is_current=False,
            end_date=date(2021, 1, 1),
        ),
        claim(
            "current",
            official.source_id,
            "Organisation B",
            identity_relevance=official.identity.score,
            fact_group="current-role",
            is_current=True,
            as_of_date=date(2026, 1, 1),
        ),
    ]

    profile = reconcile(
        "person",
        seed,
        claims,
        [official],
        ScoringPolicy(),
        date(2026, 9, 27),
    )
    organisation = profile.records[0].fields[ProfileField.organisation]

    assert organisation.value == "Organisation B"
    assert organisation.selected_claim_id == "current"
    assert organisation.alternative_claim_ids == ["historical"]
    assert {item.source_id for item in claims} == {"official"}


def test_reliable_current_official_evidence_can_correct_an_inaccurate_seed_affiliation():
    seed = PersonSeed(full_name="Jane Doe", organisation="Organisation A")
    text = (
        "Jane Doe does not work for Organisation A. "
        "Jane Doe is now Chief Executive Officer of Organisation B."
    )
    official = source("correction", seed, text)
    organisation = claim(
        "current-org",
        official.source_id,
        "Organisation B",
        identity_relevance=official.identity.score,
        fact_group="current-role",
        is_current=True,
        as_of_date=date(2026, 1, 1),
    )
    title_value = "Chief Executive Officer"
    normalised_title, certainty = normalise(ProfileField.job_title, title_value)
    title = EvidenceClaim(
        claim_id="current-title",
        person_id="person",
        source_id=official.source_id,
        field=ProfileField.job_title,
        raw_value=title_value,
        normalised_value=normalised_title,
        normalisation_certainty=certainty,
        evidence_text=f"Jane Doe is now {title_value} of Organisation B.",
        subject_name="Jane Doe",
        identity_relevance=official.identity.score,
        extraction_model="fixture",
        fact_group="current-role",
        is_current=True,
        as_of_date=date(2026, 1, 1),
        directness="explicit",
    )

    profile = reconcile(
        "person",
        seed,
        [organisation, title],
        [official],
        ScoringPolicy(),
        date(2026, 9, 27),
    )
    record = profile.records[0]

    assert not official.identity.rejected
    assert "SEED_IDENTITY_CONTRADICTION" in official.identity.reason_codes
    assert record.fields[ProfileField.organisation].value == "Organisation B"
    assert record.fields[ProfileField.job_title].value == title_value
    assert all(item.raw_value != "Organisation A" for item in [organisation, title])


def test_alumni_organisation_guides_research_but_is_not_a_current_employer_anchor():
    seed = parse_batch(
        b"name,alumni_organisation\nJane Doe,Example University Alumni Association\n",
        "people.csv",
        None,
        settings(),
    ).seeds[0]
    policy = ScoringPolicy()
    match = assess_identity(
        seed,
        "Jane Doe is a member of Example University Alumni Association.",
        policy,
    )

    assert seed.organisation is None
    assert seed.known_attributes == {"alumni_organisation": "Example University Alumni Association"}
    assert "Example University Alumni Association" in build_query(seed)
    assert seed_identity_anchors(seed) == {}
    assert match.score > policy.identity_name_only
    assert match.signals["matched_seed_anchors"] == []


def test_government_office_seed_is_a_strong_public_official_identity_clue():
    seed = parse_batch(
        b"name,government_office\nJane Doe,Ministry of Science\n",
        "people.csv",
        None,
        settings(),
    ).seeds[0]
    policy = ScoringPolicy()
    official = assess_identity(
        seed,
        "Jane Doe serves at the Ministry of Science.",
        policy,
    )
    namesake = assess_identity(seed, "Jane Doe is a professional athlete.", policy)

    assert seed.organisation == "Ministry of Science"
    assert seed_identity_anchors(seed) == {"organisation": "Ministry of Science"}
    assert official.score > namesake.score
    assert official.signals["matched_seed_anchors"] == ["organisation"]
    assert namesake.signals["matched_seed_anchors"] == []


def test_passive_source_list_metadata_never_changes_search_or_identity():
    seed = PersonSeed(
        full_name="Jane Doe",
        known_attributes={
            "LIST_NAME": "Priority prospects",
            "source_dataset": "September import",
            "alumni_organisation": "Example Alumni Network",
        },
    )
    query = build_query(seed)
    identity = assess_identity(
        seed,
        "Jane Doe appears in Priority prospects from the September import.",
        ScoringPolicy(),
    )

    assert "Example Alumni Network" in query
    assert "Priority prospects" not in query
    assert "September import" not in query
    assert identity.signals["matched_clues"] == []
    assert seed_identity_anchors(seed) == {}
    assert research_seed_payload(seed)["known_attributes"] == {
        "alumni_organisation": "Example Alumni Network"
    }
