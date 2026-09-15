"""Exercise production Store/migrations on SQLite and an optional isolated PostgreSQL schema."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.config import Settings
from app.db.models import PersonTaskRow, RetrievalCacheRow
from app.db.store import Store, normalize_database_url
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


def test_final_expired_attempt_terminates_job_and_preserves_checkpoint(store):
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    first = store.claim_task("first")
    checkpoint = result_for(first)
    assert store.checkpoint(first, checkpoint)
    expire(store, first)
    second = store.claim_task("second")
    assert second.person_id == first.person_id
    expire(store, second)
    assert store.claim_task("third") is None
    assert store.get_job(job.job_id).status == "failed"
    view = store.get_results(job.job_id).people[0]
    assert view.error_code == "WORKER_ATTEMPTS_EXHAUSTED"
    assert view.result.profile.status == "failed"
    assert view.result.profile.research_status == "failed"
    assert view.result.claims[0].claim_id == checkpoint.claims[0].claim_id
    assert not store.finish_task(second, checkpoint)


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
