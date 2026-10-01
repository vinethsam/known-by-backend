"""Adverse cases for quality recovery and the last output normalization boundary."""

import csv
import io
from datetime import date

import pytest
from openpyxl import load_workbook
from test_golden_quality import POLICY, TODAY, fact, page, profile, role

from app.export.formats import export_results
from app.research.confidence import claim_strength
from app.research.normalisation import finalize_display
from app.schemas import (
    FieldDecision,
    JobResults,
    PersonProfile,
    PersonResultView,
    PersonSeed,
    ProfileField,
    ProfileRecord,
    ResearchResult,
    SourceType,
)


@pytest.mark.parametrize("dated_context", ["claim", "news", "archive", "publication"])
def test_current_label_cannot_rejuvenate_explicitly_dated_or_archival_evidence(dated_context):
    seed = PersonSeed(full_name="Jane Doe")
    source = page("office", seed, "", secure=True, published=date(2010, 1, 1))
    observed = None
    if dated_context == "claim":
        observed = date(2010, 1, 1)
    elif dated_context == "publication":
        source.source_type = SourceType.publication
    else:
        source.final_url = f"https://office.example/{dated_context}/profile"
    result = profile(
        seed, [source], role(source, seed, "Prime Minister's Office", "Prime Minister", observed=observed)
    )
    assert result.fields[ProfileField.job_title].confidence < POLICY.review_threshold
    assert "OUTDATED_CURRENT_ROLE" in result.fields[ProfileField.job_title].review_reason_codes


def test_same_quote_with_multiple_roles_does_not_infer_crossed_pairs():
    seed = PersonSeed(full_name="Jane Doe")
    source = page("office", seed, "", secure=True)
    quote = "Jane Doe is President at Foundation A and Director at Company B."
    claims = [
        fact(source, seed, field, value, current=True, evidence=quote)
        for field, value in [
            (ProfileField.organisation, "Foundation A"),
            (ProfileField.job_title, "President"),
            (ProfileField.organisation, "Company B"),
            (ProfileField.job_title, "Director"),
        ]
    ]
    result = profile(seed, [source], claims)
    assert not (
        result.fields[ProfileField.organisation].value and result.fields[ProfileField.job_title].value
    )


def test_missing_group_does_not_join_different_quotes_on_same_page():
    seed = PersonSeed(full_name="Jane Doe")
    source = page("office", seed, "", secure=True)
    claims = [
        fact(source, seed, ProfileField.organisation, "Office A"),
        fact(source, seed, ProfileField.job_title, "Prime Minister"),
    ]
    result = profile(seed, [source], claims)
    assert not (
        result.fields[ProfileField.organisation].value and result.fields[ProfileField.job_title].value
    )


def test_old_claim_ages_once_without_an_additional_base_score_deduction():
    seed = PersonSeed(full_name="Jane Doe")
    source = page("office", seed, "", secure=True)
    recent = fact(source, seed, ProfileField.job_title, "Prime Minister", current=True, observed=TODAY)
    old = recent.model_copy(update={"as_of_date": date(2010, 1, 1)})
    fresh_score, fresh_components = claim_strength(recent, source, POLICY, TODAY)
    old_score, old_components = claim_strength(old, source, POLICY, TODAY)
    assert old_components["base"] == fresh_components["base"]
    assert old_score == pytest.approx(fresh_score * old_components["time_sensitivity"])
    assert old_score < fresh_score


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_final_gate_normalizes_every_record_and_export_without_mutating_evidence(format):
    fields = {field: FieldDecision() for field in ProfileField}
    fields[ProfileField.full_name] = FieldDecision(value="Jane Doe", confidence=100)
    fields[ProfileField.job_title] = FieldDecision(
        value="Pm", confidence=87, sources=["https://official.example/Profile"]
    )
    original = PersonProfile(
        person_id="p",
        input_name="Jane Doe",
        fields=fields,
        profile_confidence=87,
        coverage=20,
        review_required=False,
        records=[
            ProfileRecord(
                record_id="r",
                fields={key: value.model_copy(deep=True) for key, value in fields.items()},
                profile_confidence=87,
                coverage=20,
                review_required=False,
            )
        ],
    )
    before = original.model_dump()
    final = finalize_display(original)
    assert final.fields[ProfileField.job_title].value == "Prime Minister"
    assert final.records[0].fields[ProfileField.job_title].value == "Prime Minister"
    assert final.fields[ProfileField.job_title].confidence == 87
    assert final.fields[ProfileField.job_title].sources == ["https://official.example/Profile"]
    job = JobResults(
        job_id="job",
        status="completed",
        people=[
            PersonResultView(
                person_id="p",
                row_index=2,
                original_row={"LIST_NAME": "Pm"},
                status="completed",
                result=ResearchResult(profile=original),
            )
        ],
    )
    data = export_results(job, ["LIST_NAME"], format)
    if format == "csv":
        row = next(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    else:
        workbook = load_workbook(io.BytesIO(data))
        rows = list(workbook.active.values)
        row = dict(zip(rows[0], rows[1], strict=True))
        workbook.close()
    assert row["job_title"] == "Prime Minister"
    assert row["LIST_NAME"] == "Pm"
    assert original.model_dump() == before


def test_strong_current_office_wins_under_source_and_claim_permutations():
    seed = PersonSeed(full_name="Jane Doe")
    current = page("official", seed, "", secure=True, published=date(2010, 1, 1))
    old = page("old", seed, "", secure=True)
    claims = [
        *role(current, seed, "Prime Minister's Office", "Prime Minister"),
        *role(old, seed, "Company A", "Technical Analyst", current=False),
    ]
    first = profile(seed, [current, old], claims)
    second = profile(seed, [old, current], list(reversed(claims)))
    for field in [ProfileField.organisation, ProfileField.job_title, ProfileField.profile_link]:
        assert first.fields[field].value == second.fields[field].value
        assert first.fields[field].confidence == second.fields[field].confidence
