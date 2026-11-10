"""Exercise production Store/migrations on SQLite and an optional isolated PostgreSQL schema."""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.config import Settings
from app.db.library import DatabaseResultFileStorage
from app.db.models import PersonTaskRow, ProfileRow, RetrievalCacheRow
from app.db.store import Store, normalize_database_url
from app.export.artifacts import build_export_artifact
from app.library import LibraryLimitError
from app.research.telemetry import record_person_result
from app.schemas import (
    EvidenceClaim,
    FieldDecision,
    PersonProfile,
    PersonSeed,
    ProfileField,
    ResearchResult,
    SourceRecord,
    UsageRecord,
    utcnow,
)
from app.seed_context import seed_input_attributes
from app.worker import FINALIZATION_PENDING, process_lease


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path):
    admin = None
    schema = None
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'durable.db'}"
    else:
        configured = os.environ.get("TEST_POSTGRES_URL")
        if not configured:
            pytest.skip("TEST_POSTGRES_URL is not configured")
        parsed = make_url(normalize_database_url(configured))
        assert parsed.get_backend_name() == "postgresql"
        admin = create_engine(parsed)
        schema = "research_test_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        url = parsed.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(
            hide_password=False
        )
    instance = None
    try:
        settings = Settings(
            _env_file=None,
            DATABASE_URL=url,
            WORKER_MAX_ATTEMPTS=2,
            MAX_CACHE_BYTES_PER_JOB=1024,
            RETRIEVAL_CACHE_TTL_SECONDS=60,
        )
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        instance = Store(settings)
        assert instance.ready()
        yield instance
    finally:
        if instance:
            instance.engine.dispose()
        if admin:
            # Only this fixture's randomly generated schema is removed.
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            admin.dispose()


def result_for(lease, value="Jane Doe"):
    source = SourceRecord(
        person_id=lease.person_id,
        requested_url="https://example.org/person",
        final_url="https://example.org/person",
        canonical_url="https://example.org/person",
        domain="example.org",
    )
    claim = EvidenceClaim(
        person_id=lease.person_id,
        source_id=source.source_id,
        field=ProfileField.full_name,
        raw_value=value,
        normalised_value=value,
        evidence_text=value,
        subject_name=value,
        extraction_model="fixture/model",
    )
    usage = UsageRecord(
        job_id=lease.job_id,
        person_id=lease.person_id,
        source_id=source.source_id,
        role="extraction",
        model="fixture/model",
        prompt_version="v1",
        cost=0.01,
    )
    fields = {field: FieldDecision() for field in ProfileField}
    fields[ProfileField.full_name] = FieldDecision(
        value=value, selected_claim_id=claim.claim_id, supporting_claim_ids=[claim.claim_id]
    )
    return ResearchResult(
        profile=PersonProfile(
            person_id=lease.person_id,
            input_name="Jane Doe",
            status="review_required",
            fields=fields,
            profile_confidence=50,
            coverage=100 / 7,
            review_required=True,
        ),
        sources=[source],
        claims=[claim],
        usage=[usage],
    )


def expire(store, lease):
    with store.session_factory.begin() as session:
        session.get(PersonTaskRow, lease.person_id).lease_expires_at = utcnow() - timedelta(seconds=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", [True, False])
async def test_final_result_checkpoint_prevents_research_replay_or_permanent_failure_loop(
    store, monkeypatch, caplog, interrupted
):
    caplog.set_level(logging.INFO, logger="app.db.store")
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    first = store.claim_task("first")
    result = result_for(first)
    research_calls = 0

    class Pipeline:
        async def research(self, *args):
            nonlocal research_calls
            research_calls += 1
            return result

    def fail_finalization(*args):
        if interrupted:
            raise asyncio.CancelledError()
        raise DataError("test statement", {}, ValueError("test payload failure"))

    with monkeypatch.context() as patch:
        patch.setattr(store, "finish_task", fail_finalization)
        if interrupted:
            with pytest.raises(asyncio.CancelledError):
                await process_lease(store, Pipeline(), first, store.settings)
        else:
            await process_lease(store, Pipeline(), first, store.settings)

    expire(store, first)
    second = store.claim_task("second")
    if interrupted:
        assert second.checkpoint.profile.metrics.recovery[FINALIZATION_PENDING] is True
        await process_lease(store, Pipeline(), second, store.settings)
    else:
        assert second is None
    assert research_calls == 1
    assert store.claim_task("third") is None
    saved = store.get_results(job.job_id).people[0]
    assert saved.status == ("review_required" if interrupted else "failed")
    assert saved.error_code == (None if interrupted else "PERSISTENCE_FAILED")
    assert [claim.claim_id for claim in saved.result.claims] == [result.claims[0].claim_id]
    assert [usage.usage_id for usage in saved.result.usage] == [result.usage[0].usage_id]
    (summary,) = [
        record.performance["research_cost"]
        for record in caplog.records
        if record.getMessage() == "research_cost_summary"
    ]
    assert summary["reported_api_cost"] == pytest.approx(result.usage[0].cost)
    if not interrupted:
        assert saved.result.profile.research_status == "persistence_failed"
        assert summary["research_outcomes"] == {"persistence_failed": 1}
        assert summary["successfully_enriched_people"] == 0


def test_final_expired_attempt_waits_then_resumes_and_preserves_checkpoint(store):
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    first = store.claim_task("first")
    checkpoint = result_for(first)
    assert store.checkpoint(first, checkpoint)
    expire(store, first)
    second = store.claim_task("second")
    assert second.person_id == first.person_id
    expire(store, second)
    assert store.claim_task("third") is None
    waiting = store.get_job(job.job_id)
    assert waiting.status == "queued"
    assert waiting.recovery_state == "reconnecting"
    assert waiting.counts["failed"] == 0
    view = store.get_results(job.job_id).people[0]
    assert view.error_code == "INFRASTRUCTURE_WAIT:WORKER_INTERRUPTED"
    assert view.result.profile.status == "queued"
    assert view.result.profile.research_status == "queued"
    assert view.result.claims[0].claim_id == checkpoint.claims[0].claim_id
    assert not store.finish_task(second, checkpoint)
    expire(store, second)
    recovered = store.claim_task("third")
    assert recovered.person_id == first.person_id
    assert recovered.checkpoint.claims[0].claim_id == checkpoint.claims[0].claim_id
    assert store.finish_task(recovered, recovered.checkpoint)
    assert store.get_job(job.job_id).status == "completed"


def test_reclaim_retains_provenance_usage_and_does_not_duplicate_checkpoints(store):
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    first = store.claim_task("first")
    initial = result_for(first)
    assert store.checkpoint(first, initial)
    assert store.checkpoint(first, initial)
    expire(store, first)
    second = store.claim_task("second")
    final = result_for(second)
    assert store.finish_task(second, final)
    saved = store.get_results(job.job_id).people[0].result
    assert len(saved.sources) == len(saved.claims) == len(saved.usage) == 2
    assert sum(item.cost for item in saved.usage) == pytest.approx(0.02)
    assert saved.profile.fields[ProfileField.full_name].selected_claim_id == final.claims[0].claim_id
    assert all(item.source_id in {s.source_id for s in saved.sources} for item in saved.claims)


def test_terminal_write_preserves_grounded_result_with_escaped_nul(store, caplog):
    original_note = "Verbatim input\x00metadata"
    seed = PersonSeed(full_name="Jane Doe", input_metadata={"Notes": original_note})
    job = store.create_job([seed])
    lease = store.claim_task("worker")
    result = result_for(lease)
    result.profile.seed_attributes = seed_input_attributes(lease.seed)
    result.profile.fields[ProfileField.full_name].confidence = 91
    result.profile.fields[ProfileField.full_name].research_confidence = 91
    record_person_result(result)
    assert store.checkpoint(lease, result)

    if store.engine.dialect.name == "postgresql":
        with store.session_factory() as session, pytest.raises(DataError) as error:
            # Reproduce the old terminal query against the same stored payload.
            session.scalar(
                select(ProfileRow.data_json["metrics"]["accounting"]).where(
                    ProfileRow.person_id == lease.person_id
                )
            )
        assert error.value.orig.sqlstate == "22P05"

    with caplog.at_level(logging.INFO, logger="app.db.store"):
        assert store.finish_task(lease, result)
        assert store.finish_task(lease, result)  # Idempotent commit receipt.
    assert store.claim_task("other-worker") is None
    assert store.get_job(job.job_id).status == "completed"
    saved = store.get_results(job.job_id).people[0].result
    assert saved.profile.seed_attributes[0]["value"] == original_note
    assert saved.profile.fields[ProfileField.full_name].research_confidence == 91
    assert [claim.claim_id for claim in saved.claims] == [result.claims[0].claim_id]
    assert [usage.usage_id for usage in saved.usage] == [result.usage[0].usage_id]
    summaries = [
        event.performance["research_cost"]
        for event in caplog.records
        if event.getMessage() == "research_cost_summary"
    ]
    assert len(summaries) == 1
    assert summaries[0]["actual_api_cost"] == pytest.approx(0.01)
    assert summaries[0]["completed_people"] == 1
    assert summaries[0]["batch_end_to_end_ms"] >= summaries[0]["batch_processing_wall_ms"] >= 0
    assert summaries[0]["batch_end_to_end_ms"] >= summaries[0]["batch_queue_wait_ms"] >= 0
    assert summaries[0]["completed_people_per_minute"] > 0


def test_diagnostic_read_failure_keeps_terminal_result_committed(store, monkeypatch, caplog):
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    lease = store.claim_task("worker")
    result = result_for(lease)

    def unavailable(_session, _job_id):
        raise DataError(None, None, RuntimeError("Diagnostic read unavailable"))

    monkeypatch.setattr(store, "_job_cost_summary", unavailable)
    assert store.finish_task(lease, result)
    assert store.get_job(job.job_id).status == "completed"
    assert store.get_results(job.job_id).people[0].result.claims[0].claim_id == result.claims[0].claim_id
    assert "research_cost_summary_unavailable" in caplog.messages


def test_concurrent_claims_and_completions_keep_final_counts_consistent(store):
    job = store.create_job([PersonSeed(full_name=f"Person {i}") for i in range(8)])
    with ThreadPoolExecutor(max_workers=4) as pool:
        leases = [item for item in pool.map(store.claim_task, [f"worker-{i}" for i in range(8)]) if item]
        # SKIP LOCKED may deliberately return no task while another claimant holds the job lock.
        while lease := store.claim_task("remaining"):
            leases.append(lease)
        assert len(leases) == len({lease.person_id for lease in leases}) == 8
        assert all(pool.map(lambda lease: store.finish_task(lease, result_for(lease)), leases))
    finished = store.get_job(job.job_id)
    assert finished.status == "completed"
    assert finished.counts["review_required"] == 8


def test_cache_limits_expiry_and_terminal_cleanup(store):
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    store.put_cache(job.job_id, "https://example.org/a", {"body": "x" * 700})
    store.put_cache(job.job_id, "https://example.org/b", {"body": "x" * 700})
    assert store.get_cache(job.job_id, "https://example.org/a")
    assert store.get_cache(job.job_id, "https://example.org/b") is None
    with store.session_factory.begin() as session:
        row = session.scalar(select(RetrievalCacheRow).where(RetrievalCacheRow.job_id == job.job_id))
        row.updated_at = utcnow() - timedelta(seconds=61)
    assert store.get_cache(job.job_id, "https://example.org/a") is None
    store.heartbeat("cleanup")
    with store.session_factory() as session:
        assert session.scalar(select(RetrievalCacheRow)) is None
    store.put_cache(job.job_id, "https://example.org/a", {"body": "fresh"})
    assert store.cancel_job(job.job_id)
    store.put_cache(job.job_id, "https://example.org/a", {"body": "late"})
    assert store.get_cache(job.job_id, "https://example.org/a") is None


def test_cancellation_and_completion_race_never_revives_cancelled_task(store):
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    lease = store.claim_task("worker")
    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = pool.submit(store.finish_task, lease, result_for(lease))
        cancelled = pool.submit(store.cancel_job, job.job_id)
        assert completed.result() != cancelled.result()
    view = store.get_results(job.job_id)
    assert (view.status, view.people[0].status) in {
        ("completed", "review_required"),
        ("cancelled", "cancelled"),
    }
    assert not store.renew_lease(lease)


def test_library_quota_is_atomic_across_independent_storage_instances(store):
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    lease = store.claim_task("worker")
    assert store.finish_task(lease, result_for(lease))
    artifact = build_export_artifact(
        store.get_results(job.job_id), store.get_columns(job.job_id), store.get_job(job.job_id), "csv"
    )
    settings = store.settings.model_copy(update={"LIBRARY_MAX_FILES": 1})

    def save():
        adapter = DatabaseResultFileStorage(store.session_factory, settings)
        try:
            return adapter.save(artifact)
        except LibraryLimitError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(save) for _ in range(2)]
        saved = [attempt.result() for attempt in attempts]

    assert sum(item is not None for item in saved) == 1
    adapter = DatabaseResultFileStorage(store.session_factory, settings)
    rows = adapter.list_files()
    assert len(rows) == 1
    assert adapter.get(rows[0].file_id).content == artifact.content
    assert adapter.delete(rows[0].file_id)
    assert adapter.save(artifact).filename == artifact.filename


def test_raw_control_metadata_round_trips_json_and_library_labels_are_safe(store):
    original = {"full_name": "Jane\x00 Doe", "LIST_NAME": "\u00c3\u0081lumni\x00", "note": "one\vnext\fpage"}
    seed = PersonSeed(full_name=original["full_name"], input_metadata=original)
    created = store.create_job([seed], [original], list(original))
    lease = store.claim_task("offline-input-test")
    assert lease.seed.full_name == "Jane Doe"
    assert lease.seed.input_metadata == original
    assert store.get_results(created.job_id).people[0].original_row == original
    assert store.finish_task(lease, result_for(lease))
    artifact = build_export_artifact(
        store.get_results(created.job_id),
        list(original),
        store.get_job(created.job_id),
        "xlsx",
        provenance="field",
    )
    assert artifact.list_name == "Álumni"
    assert artifact.filename.startswith("Álumni_")
    adapter = DatabaseResultFileStorage(store.session_factory, store.settings)
    saved = adapter.save(artifact)
    assert adapter.get(saved.file_id).metadata.list_name == "Álumni"
    assert store.get_results(created.job_id).people[0].original_row == original
