"""Focused regressions for current-role and Wikipedia reconciliation."""

from datetime import date

from app.config import ScoringPolicy
from app.research.confidence import confidence_for_group
from app.research.normalisation import normalise
from app.research.reconciliation import reconcile
from app.schemas import EvidenceClaim, IdentityMatch, PersonSeed, ProfileField, SourceRecord, SourceType


def _source(
    identifier: str,
    *,
    source_type: SourceType,
    authority: float,
    identity: float = 1,
) -> SourceRecord:
    domain = f"{identifier}.example"
    return SourceRecord(
        source_id=identifier,
        person_id="person",
        requested_url=f"https://{domain}/profile",
        final_url=f"https://{domain}/profile",
        canonical_url=f"https://{domain}/profile",
        domain=domain,
        source_type=source_type,
        authority_score=authority,
        identity=IdentityMatch(score=identity, ambiguous=identity < 0.8),
        content_hash=identifier,
    )


def _claim(
    identifier: str,
    source_id: str,
    field: ProfileField,
    value: str,
    *,
    identity: float = 1,
    fact_group: str | None = None,
    current: bool | None = None,
    observed: date | None = None,
    ended: date | None = None,
) -> EvidenceClaim:
    normalised, certainty = normalise(field, value)
    return EvidenceClaim(
        claim_id=identifier,
        person_id="person",
        source_id=source_id,
        field=field,
        raw_value=value,
        normalised_value=normalised,
        normalisation_certainty=certainty,
        evidence_text=f"Jane Doe: {value}",
        subject_name="Jane Doe",
        identity_relevance=identity,
        extraction_model="fixture",
        fact_group=fact_group,
        is_current=current,
        as_of_date=observed,
        end_date=ended,
        directness="explicit",
    )


def _role(
    source_id: str,
    organisation: str,
    title: str,
    *,
    current: bool | None,
    observed: date | None = None,
    ended: date | None = None,
    fact_group: str | None = None,
) -> list[EvidenceClaim]:
    group = fact_group or source_id
    return [
        _claim(
            f"{source_id}-org",
            source_id,
            ProfileField.organisation,
            organisation,
            fact_group=group,
            current=current,
            observed=observed,
            ended=ended,
        ),
        _claim(
            f"{source_id}-title",
            source_id,
            ProfileField.job_title,
            title,
            fact_group=group,
            current=current,
            observed=observed,
            ended=ended,
        ),
    ]


def test_undated_explicit_current_official_role_clears_threshold_but_keeps_identity_review():
    policy = ScoringPolicy()
    government = _source(
        "government",
        source_type=SourceType.government,
        authority=policy.authority[SourceType.government],
        identity=policy.identity_name_only,
    )
    title = _claim(
        "government-title",
        government.source_id,
        ProfileField.job_title,
        "Prime Minister",
        identity=policy.identity_name_only,
        fact_group="current-office",
        current=True,
    )

    confidence, components, reasons, selected = confidence_for_group(
        [title],
        [],
        {government.source_id: government},
        policy,
        date(2026, 9, 27),
    )

    assert confidence > policy.review_threshold
    assert components["recency"] == 1
    assert selected.claim_id == title.claim_id
    assert "IDENTITY_AMBIGUITY" in reasons
    assert government.identity.ambiguous


def test_current_prime_minister_beats_old_analyst_and_current_party_role():
    policy = ScoringPolicy()
    sources = [
        _source("government", source_type=SourceType.government, authority=0.95),
        _source("analyst", source_type=SourceType.publication, authority=0.8),
        _source("party", source_type=SourceType.directory, authority=0.65),
    ]
    claims = [
        *_role(
            "government",
            "Office of the Prime Minister",
            "Prime Minister",
            current=True,
        ),
        *_role(
            "analyst",
            "Old Finance Company",
            "Financial Analyst",
            current=False,
            observed=date(2012, 1, 1),
            ended=date(2015, 12, 31),
        ),
        *_role(
            "party",
            "Example National Party",
            "Party Leader",
            current=True,
            observed=date(2026, 9, 20),
        ),
    ]

    record = reconcile(
        "person",
        PersonSeed(full_name="Jane Doe"),
        claims,
        sources,
        policy,
        date(2026, 9, 27),
    ).records[0]

    organisation = record.fields[ProfileField.organisation]
    title = record.fields[ProfileField.job_title]
    assert (organisation.value, title.value) == ("Office of the Prime Minister", "Prime Minister")
    assert organisation.supporting_source_ids == title.supporting_source_ids == ["government"]
    assert organisation.scoring_components["relationship_type"] == "current_government_office"
    assert {"analyst-org", "party-org"} <= set(organisation.alternative_claim_ids)


def test_country_value_is_rejected_when_a_government_office_is_evidenced():
    sources = [
        _source("country", source_type=SourceType.government, authority=0.95),
        _source("office", source_type=SourceType.government, authority=0.95),
    ]
    claims = [
        *_role(
            "country",
            "Ghana",
            "President",
            current=True,
            observed=date(2026, 9, 20),
        ),
        *_role(
            "office",
            "Office of the President",
            "President",
            current=True,
            observed=date(2026, 9, 20),
        ),
    ]

    record = reconcile(
        "person",
        PersonSeed(full_name="Jane Doe", country="Ghana"),
        claims,
        sources,
        ScoringPolicy(),
        date(2026, 9, 27),
    ).records[0]

    organisation = record.fields[ProfileField.organisation]
    title = record.fields[ProfileField.job_title]
    assert organisation.value == "Office of the President"
    assert title.value == "President"
    assert organisation.supporting_source_ids == ["office"]
    assert "office" in title.supporting_source_ids
    assert "country-org" in organisation.alternative_claim_ids
    assert organisation.scoring_components["public_office_type"] == "head_of_state"


def test_wikipedia_can_fill_education_missing_from_official_evidence():
    sources = [
        _source("official", source_type=SourceType.government, authority=0.95),
        _source("wikipedia", source_type=SourceType.encyclopedia, authority=0.6),
    ]
    claims = [
        *_role(
            "official",
            "Office of the President",
            "President",
            current=True,
            observed=date(2026, 9, 20),
        ),
        _claim(
            "wikipedia-university",
            "wikipedia",
            ProfileField.university_name,
            "Example University",
            fact_group="education",
        ),
        _claim(
            "wikipedia-degree",
            "wikipedia",
            ProfileField.degree_type,
            "Master of Arts",
            fact_group="education",
        ),
    ]

    record = reconcile(
        "person",
        PersonSeed(full_name="Jane Doe"),
        claims,
        sources,
        ScoringPolicy(),
        date(2026, 9, 27),
    ).records[0]

    university = record.fields[ProfileField.university_name]
    degree = record.fields[ProfileField.degree_type]
    assert university.value == "Example University"
    assert degree.value == "Master's Degree"
    assert university.supporting_source_ids == degree.supporting_source_ids == ["wikipedia"]


def test_current_official_role_beats_conflicting_wikipedia_role():
    sources = [
        _source("official", source_type=SourceType.government, authority=0.95),
        _source("wikipedia", source_type=SourceType.encyclopedia, authority=0.6),
    ]
    claims = [
        *_role(
            "official",
            "Office of the President",
            "President",
            current=True,
            observed=date(2026, 9, 20),
        ),
        *_role(
            "wikipedia",
            "Presidency of Exampleland",
            "President",
            current=True,
            observed=date(2026, 9, 26),
        ),
    ]

    record = reconcile(
        "person",
        PersonSeed(full_name="Jane Doe"),
        claims,
        sources,
        ScoringPolicy(),
        date(2026, 9, 27),
    ).records[0]

    organisation = record.fields[ProfileField.organisation]
    title = record.fields[ProfileField.job_title]
    assert organisation.value == "Office of the President"
    assert title.value == "President"
    assert organisation.selected_claim_id == "official-org"
    assert title.selected_claim_id == "official-title"
    assert organisation.supporting_source_ids == title.supporting_source_ids == ["official"]
    assert "wikipedia-org" in organisation.alternative_claim_ids
    assert "wikipedia-title" in title.alternative_claim_ids
