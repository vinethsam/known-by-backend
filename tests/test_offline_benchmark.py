"""Production pipeline benchmark regressions; no wall-clock thresholds or live calls."""

import json
from pathlib import Path

import pytest

from benchmarks.offline import run_batch


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
    assert first["result_sha256"] == baseline["batches"][0]["result_sha256"]
