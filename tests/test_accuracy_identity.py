from datetime import date

import pytest

from app.config import ScoringPolicy
from app.research.confidence import confidence_for_group
from app.research.discovery import source_authority
from app.research.identity import assess_identity, effective_identity_scores, eligible_identity_source_ids
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, PersonSeed, ProfileField, SourceRecord, SourceType


def make_source(identifier, seed, text, *, domain=None, content_hash=None):
    domain = domain or f"{identifier}.example.org"
    return SourceRecord(
        source_id=identifier,
        person_id="person",
        requested_url=f"https://{domain}/profile",
        final_url=f"https://{domain}/profile",
        canonical_url=f"https://{domain}/profile",
        domain=domain,
        content_hash=content_hash or identifier,
        source_type=SourceType.employer,
        authority_score=0.9,
        identity=assess_identity(seed, text, ScoringPolicy(), model_relevance="likely"),
    )


def make_claim(source, field, value):
    return EvidenceClaim(
        person_id="person",
        source_id=source.source_id,
        field=field,
        raw_value=value,
        normalised_value=value.casefold(),
        evidence_text=f"Jane Doe: {value}",
        subject_name="Jane Doe",
        identity_relevance=source.identity.score,
        extraction_model="fixture",
    )


def test_institution_and_normalized_title_strongly_prefer_the_seeded_person():
    seed = PersonSeed(full_name="Andrew Marsh", organisation="Entergy", job_title="CEO")
    policy = ScoringPolicy()
    correct = assess_identity(seed, "Andrew Marsh is Chief Executive Officer of Entergy.", policy)
    namesake = assess_identity(seed, "Andrew Marsh is a Wide Receiver at Example University.", policy)

    assert correct.score >= policy.identity_review_threshold
    assert {"organisation", "job_title"} <= set(correct.signals["matched_clues"])
    assert namesake.score == policy.identity_name_only
    assert namesake.ambiguous and not namesake.rejected
    assert "SEED_IDENTITY_UNCONFIRMED" in namesake.reason_codes


def test_absence_and_explicit_seed_denial_remain_distinguishable_provisional_evidence():
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    absent = assess_identity(seed, "Jane Doe studied mathematics.", ScoringPolicy())
    contradicted = assess_identity(seed, "Jane Doe never worked for Example Foundation.", ScoringPolicy())

    assert not absent.rejected
    assert absent.signals["contradicted_clues"] == []
    assert not contradicted.rejected
    assert contradicted.ambiguous
    assert "SEED_IDENTITY_CONTRADICTION" in contradicted.reason_codes
    assert "SEED_IDENTITY_UNCONFIRMED" in contradicted.reason_codes
    assert contradicted.signals["contradicted_clues"] == ["organisation"]


def test_seed_anchor_must_be_locally_associated_with_the_exact_namesake():
    seed = PersonSeed(full_name="Andrew Marsh", organisation="Entergy")
    text = (
        "Andrew Marsh is a wide receiver at Example University. "
        "In separate company news, Entergy announced quarterly earnings."
    )

    identity = assess_identity(seed, text, ScoringPolicy())

    assert identity.score == ScoringPolicy().identity_name_only
    assert identity.signals["matched_seed_anchors"] == []
    assert "SEED_IDENTITY_UNCONFIRMED" in identity.reason_codes


@pytest.mark.parametrize(
    ("text", "contradicted"),
    [
        ("Jane Doe did not leave Example Foundation.", False),
        ("Jane Doe has no affiliation with Example Foundation.", True),
        ("Jane Doe is not employed by Example Foundation.", True),
    ],
)
def test_only_explicit_affiliation_denials_mark_seed_contradictions(text, contradicted):
    identity = assess_identity(
        PersonSeed(full_name="Jane Doe", organisation="Example Foundation"),
        text,
        ScoringPolicy(),
    )

    assert not identity.rejected
    assert bool(identity.signals["contradicted_clues"]) is contradicted
    assert ("SEED_IDENTITY_CONTRADICTION" in identity.reason_codes) is contradicted


def test_not_only_and_historical_context_are_not_denials():
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    for text in (
        "Jane Doe not only works for Example Foundation but also teaches.",
        "Jane Doe previously worked for Example Foundation.",
    ):
        identity = assess_identity(seed, text, ScoringPolicy())
        assert not identity.rejected
        assert identity.signals["matched_seed_anchors"] == ["organisation"]


def test_geography_cannot_replace_an_unmatched_institutional_seed():
    seed = PersonSeed(
        full_name="Jane Doe", organisation="Example Foundation", country="Ghana", location="Accra"
    )
    identity = assess_identity(seed, "Jane Doe lives in Accra, Ghana.", ScoringPolicy())
    assert identity.score == ScoringPolicy().identity_name_only
    assert identity.signals["matched_clues"] == ["country", "location"]
    assert identity.signals["matched_seed_anchors"] == []


def test_known_attribute_cannot_overwrite_a_typed_seed_clue():
    seed = PersonSeed(
        full_name="Jane Doe",
        organisation="Example Foundation",
        known_attributes={"organisation": "Other Company", "organization": "Third Company"},
    )
    identity = assess_identity(seed, "Jane Doe leads Example Foundation.", ScoringPolicy())
    assert "organisation" in identity.signals["matched_seed_anchors"]

    namesake = assess_identity(seed, "Jane Doe leads Other Company.", ScoringPolicy())
    assert namesake.signals["matched_seed_anchors"] == []
    assert "known:organisation" not in namesake.signals["unmatched_seed_anchors"]
    assert "known:organization" not in namesake.signals["unmatched_seed_anchors"]
    assert namesake.score == ScoringPolicy().identity_name_only
    assert "SEED_IDENTITY_UNCONFIRMED" in namesake.reason_codes


def test_corroborating_namesakes_cannot_manufacture_a_seed_anchor():
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation", job_title="CEO")
    sources = {
        key: make_source(key, seed, "Jane Doe is a Wide Receiver at Example University.")
        for key in ("athlete", "sports")
    }
    claims = [
        make_claim(source, field, value)
        for source in sources.values()
        for field, value in (
            (ProfileField.university_name, "Example University"),
            (ProfileField.job_title, "Wide Receiver"),
        )
    ]
    before = {key: source.model_dump() for key, source in sources.items()}
    policy = ScoringPolicy()

    assert eligible_identity_source_ids(seed, claims, sources, policy) == set()
    assert effective_identity_scores(claims, sources, policy, seed) == {"athlete": 0.55, "sports": 0.55}
    assert before == {key: source.model_dump() for key, source in sources.items()}
    assert len(claims) == 4  # Provisional evidence is retained, never replaced with seed facts.
    record = reconcile("person", seed, claims, list(sources.values()), policy).records[0]
    assert record.fields[ProfileField.full_name].value == seed.full_name
    assert all(
        decision.value is None
        for field_name, decision in record.fields.items()
        if field_name != ProfileField.full_name
    )


def test_seed_linked_source_can_establish_an_independent_education_source():
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    primary = make_source(
        "primary", seed, "Jane Doe leads Example Foundation and attended Example University."
    )
    education = make_source("education", seed, "Jane Doe attended Example University.")
    sources = {source.source_id: source for source in (primary, education)}
    claims = [
        make_claim(source, ProfileField.university_name, "Example University") for source in sources.values()
    ]
    policy = ScoringPolicy()

    assert eligible_identity_source_ids(seed, claims, sources, policy) == set(sources)
    assert (
        effective_identity_scores(claims, sources, policy, seed)["education"]
        > policy.identity_review_threshold
    )

    education.content_hash = primary.content_hash
    assert eligible_identity_source_ids(seed, claims, sources, policy) == {"primary"}


def test_identity_bridges_do_not_chain_through_unanchored_sources():
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    sources = {
        "anchor": make_source("anchor", seed, "Jane Doe leads Example Foundation."),
        "bridge": make_source("bridge", seed, "Jane Doe is a professor."),
        "unrelated": make_source("unrelated", seed, "Jane Doe is a professor."),
    }
    claims = [
        make_claim(sources["anchor"], ProfileField.university_name, "First University"),
        make_claim(sources["bridge"], ProfileField.university_name, "First University"),
        make_claim(sources["bridge"], ProfileField.organisation, "Other Institute"),
        make_claim(sources["unrelated"], ProfileField.organisation, "Other Institute"),
    ]
    assert eligible_identity_source_ids(seed, claims, sources, ScoringPolicy()) == {"anchor", "bridge"}


def test_name_only_seed_keeps_existing_independent_context_behavior():
    seed = PersonSeed(full_name="Jane Doe")
    sources = {key: make_source(key, seed, "Jane Doe is a director.") for key in ("one", "two")}
    claims = [
        make_claim(source, ProfileField.organisation, "Example Foundation") for source in sources.values()
    ]
    policy = ScoringPolicy()
    assert eligible_identity_source_ids(seed, claims, sources, policy) == set(sources)
    assert effective_identity_scores(claims, sources, policy, seed) == effective_identity_scores(
        claims, sources, policy
    )


@pytest.mark.parametrize("host", ["wikipedia.org", "en.wikipedia.org", "en.m.wikipedia.org"])
def test_wikipedia_is_allowed_but_deterministically_secondary(host):
    policy = ScoringPolicy(domain_overrides={"wikipedia.org": SourceType.government})
    score, components = source_authority(SourceType.first_party, f"https://{host}/wiki/Jane_Doe", policy)
    assert components["source_type"] == "encyclopedia"
    assert components["deterministic_classification"] == "encyclopedia"
    assert score == policy.authority[SourceType.encyclopedia]
    assert (
        0
        < score
        < min(
            policy.authority[kind]
            for kind in (SourceType.government, SourceType.employer, SourceType.university)
        )
    )


@pytest.mark.parametrize("host", ["notwikipedia.org", "wikipedia.org.evil.example"])
def test_wikipedia_rule_respects_hostname_boundaries(host):
    policy = ScoringPolicy()
    score, components = source_authority(SourceType.first_party, f"https://{host}/profile", policy)
    assert score == policy.authority[SourceType.first_party]
    assert components["deterministic_classification"] is None


def test_wikipedia_agreement_helps_but_does_not_outweigh_primary_evidence():
    seed = PersonSeed(full_name="Jane Doe", organisation="Example Foundation")
    sources = {
        "official": make_source("official", seed, "Jane Doe leads Example Foundation."),
        "wiki": make_source("wiki", seed, "Jane Doe leads Example Foundation.", domain="wikipedia.org"),
    }
    policy = ScoringPolicy()
    for source in sources.values():
        source.authority_score, _ = source_authority(SourceType.first_party, source.final_url, policy)
    official = make_claim(sources["official"], ProfileField.organisation, "Example Foundation")
    agrees = make_claim(sources["wiki"], ProfileField.organisation, "Example Foundation")
    conflicts = make_claim(sources["wiki"], ProfileField.organisation, "Other Company")
    today = date(2026, 9, 26)
    single = confidence_for_group([official], [], sources, policy, today)[0]
    corroborated = confidence_for_group([official, agrees], [], sources, policy, today)[0]
    primary_score = confidence_for_group([official], [conflicts], sources, policy, today)[0]
    secondary_score = confidence_for_group([conflicts], [official], sources, policy, today)[0]
    assert corroborated > single
    assert primary_score > secondary_score
    record = reconcile(
        "person",
        seed,
        [official, conflicts],
        list(sources.values()),
        policy,
        today,
    ).records[0]
    assert record.fields[ProfileField.organisation].value == "Example Foundation"
    assert record.fields[ProfileField.organisation].selected_claim_id == official.claim_id
