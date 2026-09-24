"""Production pipeline benchmark regressions; no wall-clock thresholds or live calls."""

import json
from pathlib import Path

import pytest

from app.schemas import (
    EvidenceClaim,
    FieldDecision,
    JobResults,
    PersonProfile,
    PersonResultView,
    PersonStatus,
    ProfileField,
    ProfileRecord,
    ResearchResult,
    SourceRecord,
)
from benchmarks.offline import FIXTURE_VERSION, NORMALIZATION_VERSION, normalized_results, run_batch


@pytest.mark.asyncio
async def test_batch_benchmark_reuses_pages_without_reusing_person_claims(tmp_path):
    report = await run_batch(5, tmp_path, latency_ms=0)
    assert report["counts"]["fetches"] == 2
    assert report["counts"]["cache_hits"] == 8
    assert report["cache_hit_rate"] == 0.8
    assert report["result_read_queries"] == 6
    assert report["peak_concurrency"]["people"] <= 3
    assert report["counts"]["extractions"] == 10
    assert report["counts"]["searches"] == 5
    assert report["performance_events"] == 5
    assert report["performance_totals"]["extraction_calls"] == 10
    assert report["performance_totals"]["static_fetches"] == 2
    for index, person in enumerate(report["normalized_results"]["people"]):
        name = f"Person {index:03d}"
        assert person["status"] in {"completed", "review_required"}
        assert person["profile"]["fields"]["full_name"]["value"] == name
        assert person["claims"]
        assert {claim["subject_name"] for claim in person["claims"]} == {name}
        assert len(person["sources"]) == 2


@pytest.mark.asyncio
async def test_benchmark_normalized_output_is_repeatable(tmp_path):
    first_path, second_path = tmp_path / "first", tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    first = await run_batch(1, first_path, latency_ms=0)
    second = await run_batch(1, second_path, latency_ms=1)
    assert first["normalized_results"] == second["normalized_results"]
    assert first["result_sha256"] == second["result_sha256"]
    baseline = json.loads((Path(__file__).parents[1] / "benchmarks" / "baseline.json").read_text())
    assert baseline["fixture_version"] == FIXTURE_VERSION
    if baseline["normalization_version"] == NORMALIZATION_VERSION:
        assert first["result_sha256"] == baseline["batches"][0]["result_sha256"]
    else:
        assert baseline["normalization_version"] < NORMALIZATION_VERSION


def test_nested_record_references_and_order_normalize_deterministically():
    person_id = "person"
    source_a = SourceRecord(
        source_id="source-a",
        person_id=person_id,
        requested_url="https://a.example/profile",
        final_url="https://a.example/profile",
        canonical_url="https://a.example/profile",
        domain="a.example",
    )
    source_b = SourceRecord(
        source_id="source-b",
        person_id=person_id,
        requested_url="https://b.example/profile",
        final_url="https://b.example/profile",
        canonical_url="https://b.example/profile",
        domain="b.example",
    )
    claims = [
        EvidenceClaim(
            claim_id=f"claim-{suffix}",
            person_id=person_id,
            source_id=source.source_id,
            field=ProfileField.full_name,
            raw_value="Ada Lovelace",
            normalised_value="Ada Lovelace",
            evidence_text="Ada Lovelace",
            subject_name="Ada Lovelace",
            extraction_model="fixture/model",
        )
        for suffix, source in (("a", source_a), ("b", source_b))
    ]

    def decisions(selected):
        result = {field: FieldDecision(review_reason_codes=["MISSING_FIELD"]) for field in ProfileField}
        result[ProfileField.full_name] = FieldDecision(
            value="Ada Lovelace",
            confidence=92,
            selected_claim_id=selected,
            supporting_claim_ids=["claim-a", "claim-b"],
            supporting_source_ids=["source-a", "source-b"],
            sources=[source_a.final_url, source_b.final_url],
            review_required=False,
        )
        return result

    def job(selected, *, reversed_records):
        bachelor = ProfileRecord(
            record_id="bachelor",
            fields=decisions(selected),
            profile_confidence=92,
            coverage=14.29,
            review_required=True,
        )
        master = ProfileRecord(
            record_id="master",
            fields=decisions("claim-b"),
            profile_confidence=92,
            coverage=14.29,
            review_required=True,
        )
        records = [master, bachelor] if reversed_records else [bachelor, master]
        profile = PersonProfile(
            person_id=person_id,
            input_name="Ada Lovelace",
            fields=decisions("claim-a"),
            profile_confidence=92,
            coverage=14.29,
            review_required=True,
            records=records,
        )
        return JobResults(
            job_id="job",
            status="completed",
            people=[
                PersonResultView(
                    person_id=person_id,
                    row_index=2,
                    original_row={},
                    status=PersonStatus.review_required,
                    result=ResearchResult(
                        profile=profile,
                        sources=[source_a, source_b],
                        claims=claims,
                    ),
                )
            ],
        )

    first = normalized_results(job("claim-a", reversed_records=False))
    second = normalized_results(job("claim-b", reversed_records=True))

    assert first == second
    assert [record["record_id"] for record in first["people"][0]["profile"]["records"]] == [
        "bachelor",
        "master",
    ]
    selected = first["people"][0]["profile"]["records"][0]["fields"]["full_name"]["selected_claim_id"]
    assert selected not in {"claim-a", "claim-b"}
