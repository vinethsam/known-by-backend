from __future__ import annotations

import io
from datetime import timedelta

import pytest
from alembic.config import Config
from sqlalchemy import event, select, text
from sqlalchemy.dialects import postgresql

from alembic import command
from app.config import Settings
from app.db.models import EvidenceClaimRow, PersonTaskRow, ProfileRow, SourceRow, UsageRecordRow
from app.db.store import Store
from app.schemas import (
    EvidenceClaim,
    FieldDecision,
    JobStatus,
    PersonProfile,
    PersonSeed,
    PersonStatus,
    ProfileField,
    ProfileRecord,
    ResearchMetrics,
    ResearchResult,
    SourceRecord,
    SourceType,
    UsageRecord,
    utcnow,
)


def _settings(tmp_path, **overrides):
    data = {
        "APP_ENV": "test",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
        "WORKER_LEASE_SECONDS": 10,
        "WORKER_HEARTBEAT_SECONDS": 2,
        "WORKER_MAX_ATTEMPTS": 2,
    }
    data.update(overrides)
    return Settings(**data)


def _migrate(settings):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    command.upgrade(cfg, "head")


def _store(tmp_path, **overrides):
    settings = _settings(tmp_path, **overrides)
    _migrate(settings)
    return Store(settings)


def _result(person_id: str, status: PersonStatus = PersonStatus.completed) -> ResearchResult:
    fields = {
        field: FieldDecision(
            value=f"{field.value} value",
            confidence=90,
            review_required=False,
            sources=["https://example.com/profile"],
        )
        for field in ProfileField
    }
    profile = PersonProfile(
        person_id=person_id,
        input_name="Ada Lovelace",
        status=status,
        fields=fields,
        profile_confidence=91,
        coverage=100,
        review_required=status == PersonStatus.review_required,
        sources_considered=1,
        sources_used=1,
        metrics=ResearchMetrics(llm_calls=1, tokens_used=10),
    )
    source = SourceRecord(
        person_id=person_id,
        requested_url="https://example.com/profile",
        final_url="https://example.com/profile",
        canonical_url="https://example.com/profile",
        domain="example.com",
        source_type=SourceType.directory,
        authority_score=0.7,
    )
    usage = UsageRecord(
        job_id="job-placeholder",
        person_id=person_id,
        role="extract",
        model="test-model",
        prompt_version="test-v1",
    )
    return ResearchResult(profile=profile, sources=[source], claims=[], usage=[usage])


def test_create_claim_finish_and_results_round_trip(tmp_path):
    store = _store(tmp_path)
    assert store.ready()
    created = store.create_job(
        [PersonSeed(full_name="Ada Lovelace")],
        original_rows=[{"__row_index": "user data", "name": "Ada Lovelace", "note": "math"}],
        columns=["name", "note"],
    )

    lease = store.claim_task("worker-1")
    assert lease is not None
    assert lease.job_id == created.job_id
    assert lease.seed.full_name == "Ada Lovelace"
    result = _result(lease.person_id)
    result.usage[0].job_id = lease.job_id
    assert store.finish_task(lease, result)

    job = store.get_job(created.job_id)
    assert job is not None
    assert job.status == JobStatus.completed
    assert job.counts[PersonStatus.completed.value] == 1
    results = store.get_results(created.job_id)
    assert results is not None
    assert results.people[0].row_index == 2
    assert results.people[0].original_row == {
        "__row_index": "user data",
        "name": "Ada Lovelace",
        "note": "math",
    }
    assert results.people[0].result.profile.profile_confidence == 91
    assert results.people[0].result.sources[0].canonical_url == "https://example.com/profile"
    assert store.get_columns(created.job_id) == ["name", "note"]


def test_profile_records_survive_checkpoint_and_final_result_without_migration(tmp_path):
    store = _store(tmp_path)
    created = store.create_job([PersonSeed(full_name="Ada Lovelace")])
    lease = store.claim_task("worker-1")
    assert lease is not None
    result = _result(lease.person_id)
    result.usage[0].job_id = lease.job_id

    def record(record_id, degree, university, source_url):
        fields = {field: decision.model_copy(deep=True) for field, decision in result.profile.fields.items()}
        fields[ProfileField.degree_type] = FieldDecision(
            value=degree,
            confidence=92,
            selected_claim_id=f"{record_id}-degree",
            supporting_claim_ids=[f"{record_id}-degree"],
            supporting_source_ids=[f"{record_id}-source"],
            sources=[source_url],
            review_required=False,
        )
        fields[ProfileField.university_name] = FieldDecision(
            value=university,
            confidence=91,
            selected_claim_id=f"{record_id}-university",
            supporting_claim_ids=[f"{record_id}-university"],
            supporting_source_ids=[f"{record_id}-source"],
            sources=[source_url],
            review_required=False,
        )
        return ProfileRecord(
            record_id=record_id,
            fields=fields,
            profile_confidence=91.5,
            coverage=100,
            review_required=False,
            status=PersonStatus.completed,
            sources_used=1,
        )

    result.profile.records = [
        record("bachelors", "Bachelor's", "University A", "https://a.example/ada"),
        record("masters", "Master's", "University B", "https://b.example/ada"),
        record("doctorate", "PhD", "University C", "https://c.example/ada"),
    ]
    expected = [item.model_dump(mode="json") for item in result.profile.records]

    assert store.checkpoint(lease, result)
    checkpoint = store.get_results(created.job_id).people[0].result
    assert [item.model_dump(mode="json") for item in checkpoint.profile.records] == expected
    with store.session_factory() as session:
        persisted = session.get(ProfileRow, lease.person_id)
        assert persisted.data_json["records"] == expected

    result.profile.records[1].fields[ProfileField.subject] = FieldDecision(
        value="Mathematics",
        confidence=90,
        sources=["https://b.example/ada"],
        review_required=False,
    )
    assert store.finish_task(lease, result)
    final = store.get_results(created.job_id).people[0].result
    assert final.profile.records == result.profile.records
    assert final.profile.records[1].fields[ProfileField.subject].value == "Mathematics"


def test_legacy_profile_json_without_records_uses_empty_default():
    profile = _result("legacy-person").profile
    legacy = profile.model_dump(mode="json", exclude={"records"})

    restored = PersonProfile.model_validate(legacy)

    assert restored.records == []
    assert restored.fields == profile.fields


def test_lease_recovery_fencing_and_max_attempts(tmp_path):
    store = _store(tmp_path, WORKER_MAX_ATTEMPTS=2)
    created = store.create_job([PersonSeed(full_name="Grace Hopper")])
    first = store.claim_task("worker-1")
    assert first is not None

    with store.session_factory.begin() as session:
        task = session.scalar(select(PersonTaskRow).where(PersonTaskRow.person_id == first.person_id))
        task.lease_expires_at = utcnow() - timedelta(seconds=1)

    second = store.claim_task("worker-2")
    assert second is not None
    assert second.person_id == first.person_id
    assert second.token != first.token
    assert not store.renew_lease(first)
    assert not store.finish_task(first, _result(first.person_id))
    assert store.fail_task(second, "timeout")
    assert store.claim_task("worker-3") is None

    job = store.get_job(created.job_id)
    assert job.status == JobStatus.failed


def test_checkpoint_is_fenced_and_replaced_on_reclaim(tmp_path):
    store = _store(tmp_path, WORKER_MAX_ATTEMPTS=2)
    created = store.create_job([PersonSeed(full_name="Grace Hopper")])
    first = store.claim_task("worker-1")
    assert first is not None
    first_result = _result(first.person_id)
    first_result.profile.fields[ProfileField.job_title].value = "first checkpoint"
    assert store.checkpoint(first, first_result)

    job = store.get_job(created.job_id)
    assert job.status == JobStatus.running
    view = store.get_results(created.job_id).people[0]
    assert view.status == PersonStatus.researching
    assert view.result.profile.fields[ProfileField.job_title].value == "first checkpoint"

    with store.session_factory.begin() as session:
        task = session.scalar(select(PersonTaskRow).where(PersonTaskRow.person_id == first.person_id))
        task.lease_expires_at = utcnow() - timedelta(seconds=1)

    second = store.claim_task("worker-2")
    assert second is not None
    assert not store.checkpoint(first, first_result)
    second_result = _result(second.person_id)
    second_result.profile.fields[ProfileField.job_title].value = "second checkpoint"
    assert store.checkpoint(second, second_result)
    assert store.finish_task(second, second_result)
    final_view = store.get_results(created.job_id).people[0]
    assert final_view.result.profile.fields[ProfileField.job_title].value == "second checkpoint"


def test_cancel_fences_late_results_and_cache_upserts(tmp_path):
    store = _store(tmp_path)
    created = store.create_job([PersonSeed(full_name="Katherine Johnson")])
    lease = store.claim_task("worker-1")
    assert lease is not None
    assert store.cancel_job(created.job_id)
    assert not store.finish_task(lease, _result(lease.person_id))

    job = store.get_job(created.job_id)
    assert job.status == JobStatus.cancelled
    results = store.get_results(created.job_id)
    assert results.people[0].status == PersonStatus.cancelled
    assert results.people[0].result is None

    active = store.create_job([PersonSeed(full_name="Another Person")])
    store.put_cache(active.job_id, "https://example.com", {"text": "first"})
    store.put_cache(active.job_id, "https://example.com", {"text": "second"})
    assert store.get_cache(active.job_id, "https://example.com") == {"text": "second"}
    assert store.get_cache(created.job_id, "https://example.com") is None


def test_review_required_counts_as_successful_job_completion(tmp_path):
    store = _store(tmp_path)
    created = store.create_job([PersonSeed(full_name="Alan Turing")])
    lease = store.claim_task("worker-1")
    assert lease is not None
    assert store.finish_task(lease, _result(lease.person_id, PersonStatus.review_required))
    assert store.get_job(created.job_id).status == JobStatus.completed


@pytest.mark.parametrize("people_count", [1, 8])
def test_batch_results_use_constant_queries_and_preserve_attempt_ledgers(tmp_path, people_count):
    store = _store(tmp_path)
    seeds = [PersonSeed(full_name=f"Person {index}") for index in range(people_count)]
    originals = [{"name": seed.full_name, "note": str(index)} for index, seed in enumerate(seeds)]
    created = store.create_job(seeds, originals, ["name", "note"])
    expected = []
    for index in range(people_count):
        lease = store.claim_task("first-worker")
        attempts = []
        for attempt in range(2):
            result = _result(
                lease.person_id, PersonStatus.review_required if index % 2 else PersonStatus.completed
            )
            result.profile.input_name = lease.seed.full_name
            result.usage[0].job_id = lease.job_id
            result.claims.append(
                EvidenceClaim(
                    person_id=lease.person_id,
                    source_id=result.sources[0].source_id,
                    field=ProfileField.full_name,
                    raw_value=lease.seed.full_name,
                    normalised_value=lease.seed.full_name,
                    evidence_text=lease.seed.full_name,
                    subject_name=lease.seed.full_name,
                    extraction_model="fixture/model",
                )
            )
            attempts.append(result)
            if attempt == 0:
                assert store.checkpoint(lease, result)
                with store.session_factory.begin() as session:
                    task = session.get(PersonTaskRow, lease.person_id)
                    task.lease_expires_at = utcnow() - timedelta(seconds=1)
                lease = store.claim_task("recovery-worker")
                assert lease.person_id == result.profile.person_id
            else:
                assert store.finish_task(lease, result)
        expected.append(
            ResearchResult(
                profile=attempts[-1].profile,
                sources=[source for result in attempts for source in result.sources],
                claims=[claim for result in attempts for claim in result.claims],
                usage=[usage for result in attempts for usage in result.usage],
            )
        )

    statements = []

    def count_selects(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", count_selects)
    try:
        results = store.get_results(created.job_id)
    finally:
        event.remove(store.engine, "before_cursor_execute", count_selects)

    assert len(statements) == 6
    assert results.status == JobStatus.completed
    assert len(results.people) == people_count
    for index, (view, result) in enumerate(zip(results.people, expected, strict=True)):
        assert view.row_index == index + 2
        assert view.original_row == originals[index]
        assert view.status == result.profile.status
        assert view.error_code is None
        assert view.result.model_dump() == result.model_dump()


def test_alembic_upgrade_and_downgrade(tmp_path):
    settings = _settings(tmp_path)
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    command.upgrade(cfg, "head")
    store = Store(settings)
    with store.engine.connect() as conn:
        tables = conn.execute(text("select name from sqlite_master where type='table'")).scalars().all()
    assert "jobs" in tables
    command.downgrade(cfg, "base")
    with store.engine.connect() as conn:
        tables = conn.execute(text("select name from sqlite_master where type='table'")).scalars().all()
    assert "jobs" not in tables


def test_postgresql_migration_and_skip_locked_sql_compile():
    buffer = io.StringIO()
    cfg = Config("alembic.ini", output_buffer=buffer)
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://user:pass@example.invalid/db")
    command.upgrade(cfg, "head", sql=True)
    assert "CREATE TABLE jobs" in buffer.getvalue()

    sql = str(select(PersonTaskRow).with_for_update(skip_locked=True).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql


@pytest.mark.parametrize("claim_count", [1, 100])
def test_checkpoint_batches_ledger_writes_and_preserves_creation_order(tmp_path, claim_count):
    store = _store(tmp_path)
    store.create_job([PersonSeed(full_name="Ada Lovelace")])
    lease = store.claim_task("worker")
    assert lease.created_at is not None
    result = _result(lease.person_id)
    result.usage[0].job_id = lease.job_id
    result.claims = [
        EvidenceClaim(
            person_id=lease.person_id,
            source_id=result.sources[0].source_id,
            field=ProfileField.full_name,
            raw_value=f"Ada Lovelace {index}",
            normalised_value=f"Ada Lovelace {index}",
            evidence_text="Ada Lovelace",
            subject_name="Ada Lovelace",
            extraction_model="fixture/model",
        )
        for index in range(claim_count)
    ]
    assert store.checkpoint(lease, result)
    with store.session_factory() as session:
        original_times = {
            row_type: session.scalars(select(row_type.created_at)).all()
            for row_type in (SourceRow, EvidenceClaimRow, UsageRecordRow)
        }
    with store.session_factory.begin() as session:
        session.execute(text("CREATE TABLE ledger_updates (table_name TEXT NOT NULL)"))
        for table_name in ("sources", "evidence_claims", "usage_records"):
            session.execute(
                text(
                    f"CREATE TRIGGER count_{table_name}_updates AFTER UPDATE ON {table_name} "
                    f"BEGIN INSERT INTO ledger_updates VALUES ('{table_name}'); END"
                )
            )

    statements = []

    def capture(_connection, _cursor, sql, _parameters, _context, _executemany):
        statements.append(sql)

    result.sources[0].processing_status = "extracted"
    result.claims[0].evidence_location = "paragraph 1"
    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        assert store.checkpoint(lease, result)
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)

    assert len(statements) == 9
    assert sum(sql.lstrip().upper().startswith("SELECT") for sql in statements) == 2
    with store.session_factory() as session:
        for row_type, timestamps in original_times.items():
            assert session.scalars(select(row_type.created_at)).all() == timestamps
    stored = store.get_results(lease.job_id).people[0].result
    assert stored.sources[0].processing_status == "extracted"
    assert stored.claims[0].evidence_location == "paragraph 1"
    assert len(stored.claims) == claim_count
    assert len(stored.usage) == 1
    assert store.checkpoint(lease, result)
    with store.session_factory() as session:
        # Only the two deliberately changed records were written; unchanged claims
        # and usage, and the entire repeated checkpoint, performed no row updates.
        assert sorted(session.scalars(text("SELECT table_name FROM ledger_updates"))) == [
            "evidence_claims",
            "sources",
        ]


def test_postgresql_ledger_upsert_keeps_created_at_out_of_update(tmp_path):
    store = _store(tmp_path)
    captured = []

    class CaptureSession:
        def execute(self, statement, rows):
            captured.append(str(statement.compile(dialect=postgresql.dialect())))

    original_name = store.engine.dialect.name
    try:
        store.engine.dialect.name = "postgresql"
        store._upsert_rows(CaptureSession(), SourceRow, "source_id", [{"source_id": "fixture"}])
    finally:
        store.engine.dialect.name = original_name
    conflict = captured[0].split("ON CONFLICT", maxsplit=1)[1]
    assert "source_id" in conflict and "DO UPDATE SET" in conflict
    assert "created_at" not in conflict
    assert "IS DISTINCT FROM" in conflict and "CAST(sources.data_json AS TEXT)" in conflict
