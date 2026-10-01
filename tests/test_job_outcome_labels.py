"""Mixed terminal results keep stable status codes and a truthful user-facing label."""

from fastapi.testclient import TestClient
from test_db import _result, _store

from app.api.routes import create_app
from app.schemas import PersonSeed


def test_completed_with_issues_retains_successful_results_and_accurate_counts(tmp_path):
    store = _store(tmp_path)
    job = store.create_job([PersonSeed(full_name="Ada Lovelace"), PersonSeed(full_name="Grace Hopper")])
    successful = store.claim_task("worker")
    assert store.finish_task(successful, _result(successful.person_id))
    failed = store.claim_task("worker")
    assert store.fail_task(failed, "EXTRACTION_FAILED")
    with TestClient(create_app(store.settings, store)) as client:
        view = client.get(f"/v1/jobs/{job.job_id}").json()
        results = client.get(f"/v1/jobs/{job.job_id}/results").json()
        assert view["status"] == results["status"] == "partial"
        assert view["status_label"] == results["status_label"] == "Completed with issues"
        assert view["completed_at"] is not None
        assert view["counts"]["completed"] == view["counts"]["failed"] == 1
        assert results["people"][0]["result"] is not None
        assert results["people"][1]["error_code"] == "EXTRACTION_FAILED"
        assert client.get(f"/v1/jobs/{job.job_id}/export?format=csv").status_code == 200
