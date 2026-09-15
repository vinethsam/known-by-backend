from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, delete, event, exists, func, inspect, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    EvidenceClaimRow,
    FieldDecisionRow,
    JobRow,
    PersonTaskRow,
    ProfileRow,
    RetrievalCacheRow,
    SourceRow,
    UsageRecordRow,
    WorkerHeartbeatRow,
)
from app.schemas import (
    EvidenceClaim,
    JobCreated,
    JobResults,
    JobStatus,
    JobView,
    PersonProfile,
    PersonResultView,
    PersonSeed,
    PersonStatus,
    ResearchResult,
    SourceRecord,
    UsageRecord,
    new_id,
    utcnow,
)

EXPECTED_ALEMBIC_REVISION = "202609150001"
SUCCESS_STATUSES = {PersonStatus.completed.value, PersonStatus.review_required.value}
TERMINAL_STATUSES = SUCCESS_STATUSES | {PersonStatus.failed.value, PersonStatus.cancelled.value}


@dataclass(frozen=True)
class Lease:
    job_id: str
    person_id: str
    seed: PersonSeed
    token: str


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        url = normalize_database_url(settings.DATABASE_URL)
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _set_sqlite_foreign_keys)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings)
        self.engine = self.database.engine
        self.session_factory = self.database.session_factory

    def create_job(
        self,
        seeds: list[PersonSeed],
        original_rows: list[dict] | None = None,
        columns: list[str] | None = None,
    ) -> JobCreated:
        if not seeds or len(seeds) > self.settings.MAX_BATCH_ROWS:
            raise ValueError("Job person count is outside configured limits")
        if original_rows is not None and len(original_rows) != len(seeds):
            raise ValueError("Original rows must match the person count")
        job_id = new_id()
        now = utcnow()
        rows = original_rows or [{} for _ in seeds]
        stored_columns = list(columns or [])
        with self.session_factory.begin() as session:
            job = JobRow(
                job_id=job_id,
                status=JobStatus.queued.value,
                total_people=len(seeds),
                columns_json=stored_columns,
                created_at=now,
            )
            session.add(job)
            session.flush()
            for offset, seed in enumerate(seeds):
                source_row = dict(rows[offset]) if offset < len(rows) else {}
                session.add(
                    PersonTaskRow(
                        person_id=new_id(),
                        job_id=job_id,
                        row_index=offset + 2,
                        seed_json=seed.model_dump(mode="json"),
                        original_row_json=source_row,
                        status=PersonStatus.queued.value,
                        attempts=0,
                        created_at=now,
                    )
                )
            self._refresh_job_status(session, job)
        return JobCreated(job_id=job_id, status=JobStatus.queued, total_people=len(seeds))

    def get_job(self, id: str) -> JobView | None:
        with self.session_factory() as session:
            job = session.get(JobRow, id)
            if not job:
                return None
            counts = self._counts(session, id)
            return JobView(
                job_id=job.job_id,
                status=JobStatus(job.status),
                total_people=job.total_people,
                counts=counts,
                created_at=_aware(job.created_at),
                started_at=_aware(job.started_at),
                completed_at=_aware(job.completed_at),
            )

    def get_results(self, id: str) -> JobResults | None:
        with self.session_factory() as session:
            job = session.get(JobRow, id)
            if not job:
                return None
            people = session.scalars(
                select(PersonTaskRow).where(PersonTaskRow.job_id == id).order_by(PersonTaskRow.row_index)
            ).all()
            views = [self._person_result_view(session, task) for task in people]
            return JobResults(job_id=id, status=JobStatus(job.status), people=views)

    def get_columns(self, id: str) -> list[str]:
        with self.session_factory() as session:
            job = session.get(JobRow, id)
            return list(job.columns_json) if job else []

    def claim_task(self, worker_id: str) -> Lease | None:
        now = utcnow()
        token = str(uuid4())
        expires = now + timedelta(seconds=self.settings.WORKER_LEASE_SECONDS)
        with self.session_factory.begin() as session:
            self._heartbeat(session, worker_id, now)
            self._recover_exhausted(session, now)
            if self.engine.dialect.name == "postgresql":
                task = self._claim_postgresql(session, worker_id, token, now, expires)
            else:
                task = self._claim_guarded(session, worker_id, token, now, expires)
            if not task:
                return None
            return Lease(
                job_id=task.job_id,
                person_id=task.person_id,
                seed=PersonSeed.model_validate(task.seed_json),
                token=token,
            )

    def renew_lease(self, lease: Lease) -> bool:
        now = utcnow()
        expires = now + timedelta(seconds=self.settings.WORKER_LEASE_SECONDS)
        with self.session_factory.begin() as session:
            job = self._lock_job(session, lease.job_id)
            if not job or job.status == JobStatus.cancelled.value:
                return False
            rows = session.execute(
                update(PersonTaskRow)
                .where(
                    PersonTaskRow.person_id == lease.person_id,
                    PersonTaskRow.job_id == lease.job_id,
                    PersonTaskRow.status == PersonStatus.researching.value,
                    PersonTaskRow.lease_token == lease.token,
                    PersonTaskRow.lease_expires_at > now,
                )
                .values(lease_expires_at=expires)
                .execution_options(synchronize_session=False)
            ).rowcount
            return rows == 1

    def finish_task(self, lease: Lease, result: ResearchResult) -> bool:
        with self.session_factory.begin() as session:
            job = self._lock_job(session, lease.job_id)
            if not job or job.status == JobStatus.cancelled.value:
                return False
            task = self._locked_task_for_completion(session, lease)
            if not task:
                return False
            now = utcnow()
            task.status = result.profile.status.value
            task.error_code = None
            task.lease_token = None
            task.leased_by = None
            task.lease_expires_at = None
            task.completed_at = now
            self._replace_result(session, lease.job_id, lease.person_id, result, now)
            self._refresh_job_status(session, job)
            return True

    def checkpoint(self, lease: Lease, result: ResearchResult) -> bool:
        with self.session_factory.begin() as session:
            job = self._lock_job(session, lease.job_id)
            if not job or job.status == JobStatus.cancelled.value:
                return False
            task = self._locked_task_for_completion(session, lease)
            if not task:
                return False
            self._replace_result(session, lease.job_id, lease.person_id, result, utcnow())
            return True

    def fail_task(self, lease: Lease, error_code: str) -> bool:
        with self.session_factory.begin() as session:
            job = self._lock_job(session, lease.job_id)
            if not job or job.status == JobStatus.cancelled.value:
                return False
            task = self._locked_task_for_completion(session, lease)
            if not task:
                return False
            task.status = PersonStatus.failed.value
            task.error_code = error_code[:120]
            task.lease_token = None
            task.leased_by = None
            task.lease_expires_at = None
            task.completed_at = utcnow()
            self._refresh_job_status(session, job)
            return True

    def cancel_job(self, id: str) -> bool:
        now = utcnow()
        with self.session_factory.begin() as session:
            job = self._lock_job(session, id)
            if not job:
                return False
            if job.status == JobStatus.cancelled.value:
                return True
            if job.status in {JobStatus.completed.value, JobStatus.failed.value, JobStatus.partial.value}:
                return False
            job.status = JobStatus.cancelled.value
            job.completed_at = now
            session.execute(
                update(PersonTaskRow)
                .where(PersonTaskRow.job_id == id, PersonTaskRow.status.not_in(TERMINAL_STATUSES))
                .values(
                    status=PersonStatus.cancelled.value,
                    lease_token=None,
                    leased_by=None,
                    lease_expires_at=None,
                    completed_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            session.execute(delete(RetrievalCacheRow).where(RetrievalCacheRow.job_id == id))
            return True

    def heartbeat(self, worker_id: str) -> None:
        with self.session_factory.begin() as session:
            self._heartbeat(session, worker_id, utcnow())
            session.execute(
                delete(RetrievalCacheRow).where(
                    RetrievalCacheRow.updated_at
                    < utcnow() - timedelta(seconds=self.settings.RETRIEVAL_CACHE_TTL_SECONDS)
                )
            )

    def ready(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(select(1))
                tables = set(inspect(conn).get_table_names())
                required = {
                    "alembic_version",
                    "jobs",
                    "person_tasks",
                    "sources",
                    "evidence_claims",
                    "field_decisions",
                    "profiles",
                    "usage_records",
                    "retrieval_cache",
                    "worker_heartbeats",
                }
                if not required.issubset(tables):
                    return False
                version = conn.execute(text("select version_num from alembic_version")).scalar()
                return version == EXPECTED_ALEMBIC_REVISION
            return True
        except SQLAlchemyError:
            return False

    def worker_is_alive(self) -> bool:
        threshold = utcnow() - timedelta(seconds=self.settings.WORKER_HEARTBEAT_SECONDS * 2)
        with self.session_factory() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(WorkerHeartbeatRow)
                    .where(WorkerHeartbeatRow.last_seen_at >= threshold)
                )
                or 0
            ) > 0

    def get_cache(self, job_id: str, url: str) -> dict | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(RetrievalCacheRow).where(
                    RetrievalCacheRow.job_id == job_id,
                    RetrievalCacheRow.url == url,
                    RetrievalCacheRow.updated_at
                    >= utcnow() - timedelta(seconds=self.settings.RETRIEVAL_CACHE_TTL_SECONDS),
                )
            )
            return dict(row.payload_json) if row else None

    def put_cache(self, job_id: str, url: str, payload: dict) -> None:
        now = utcnow()
        with self.session_factory.begin() as session:
            job = self._lock_job(session, job_id)
            if not job or job.status in {"completed", "partial", "failed", "cancelled"}:
                return
            size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            cached = session.scalars(
                select(RetrievalCacheRow.payload_json).where(
                    RetrievalCacheRow.job_id == job_id,
                    RetrievalCacheRow.url != url,
                    RetrievalCacheRow.updated_at
                    >= now - timedelta(seconds=self.settings.RETRIEVAL_CACHE_TTL_SECONDS),
                )
            )
            size += sum(len(json.dumps(item, ensure_ascii=False).encode("utf-8")) for item in cached)
            if size > self.settings.MAX_CACHE_BYTES_PER_JOB:
                return
            if self.engine.dialect.name == "postgresql":
                stmt = pg_insert(RetrievalCacheRow).values(
                    job_id=job_id,
                    url=url,
                    payload_json=payload,
                    created_at=now,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_retrieval_cache_job_url",
                    set_={"payload_json": payload, "updated_at": now},
                )
            else:
                stmt = sqlite_insert(RetrievalCacheRow).values(
                    job_id=job_id,
                    url=url,
                    payload_json=payload,
                    created_at=now,
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["job_id", "url"],
                    set_={"payload_json": payload, "updated_at": now},
                )
            session.execute(stmt)

    def _recover_exhausted(self, session: Session, now: datetime) -> None:
        exhausted = (
            (PersonTaskRow.status == PersonStatus.researching.value)
            & (PersonTaskRow.lease_expires_at <= now)
            & (PersonTaskRow.attempts >= self.settings.WORKER_MAX_ATTEMPTS)
        )
        query = (
            select(JobRow)
            .where(
                JobRow.status.in_([JobStatus.queued.value, JobStatus.running.value]),
                exists(
                    select(PersonTaskRow.person_id).where(PersonTaskRow.job_id == JobRow.job_id, exhausted)
                ),
            )
            .order_by(JobRow.created_at, JobRow.job_id)
            .limit(1)
        )
        if self.engine.dialect.name == "postgresql":
            query = query.with_for_update(of=JobRow, skip_locked=True)
        job = session.scalar(query)
        if job is None:
            return
        session.execute(
            update(PersonTaskRow)
            .where(PersonTaskRow.job_id == job.job_id, exhausted)
            .values(
                status=PersonStatus.failed.value,
                error_code="WORKER_ATTEMPTS_EXHAUSTED",
                lease_token=None,
                leased_by=None,
                lease_expires_at=None,
                completed_at=now,
            )
        )
        self._refresh_job_status(session, job)

    def _claim_postgresql(
        self, session: Session, worker_id: str, token: str, now: datetime, expires: datetime
    ) -> PersonTaskRow | None:
        job = session.scalar(
            select(JobRow)
            .where(
                JobRow.status.not_in(
                    [JobStatus.cancelled.value, JobStatus.completed.value, JobStatus.failed.value]
                ),
                exists(
                    select(PersonTaskRow.person_id).where(
                        PersonTaskRow.job_id == JobRow.job_id,
                        PersonTaskRow.attempts < self.settings.WORKER_MAX_ATTEMPTS,
                        (
                            (PersonTaskRow.status == PersonStatus.queued.value)
                            | (
                                (PersonTaskRow.status == PersonStatus.researching.value)
                                & (PersonTaskRow.lease_expires_at <= now)
                            )
                        ),
                    )
                ),
            )
            .order_by(JobRow.created_at, JobRow.job_id)
            .with_for_update(of=JobRow, skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        candidate = session.scalar(
            select(PersonTaskRow)
            .where(
                PersonTaskRow.job_id == job.job_id,
                PersonTaskRow.attempts < self.settings.WORKER_MAX_ATTEMPTS,
                (
                    (PersonTaskRow.status == PersonStatus.queued.value)
                    | (
                        (PersonTaskRow.status == PersonStatus.researching.value)
                        & (PersonTaskRow.lease_expires_at <= now)
                    )
                ),
            )
            .order_by(PersonTaskRow.created_at, PersonTaskRow.row_index)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not candidate:
            return None
        self._mark_claimed(session, job, candidate, worker_id, token, now, expires)
        return candidate

    def _claim_guarded(
        self, session: Session, worker_id: str, token: str, now: datetime, expires: datetime
    ) -> PersonTaskRow | None:
        candidate = session.scalar(
            select(PersonTaskRow)
            .join(JobRow, JobRow.job_id == PersonTaskRow.job_id)
            .where(
                JobRow.status != JobStatus.cancelled.value,
                PersonTaskRow.attempts < self.settings.WORKER_MAX_ATTEMPTS,
                (
                    (PersonTaskRow.status == PersonStatus.queued.value)
                    | (
                        (PersonTaskRow.status == PersonStatus.researching.value)
                        & (PersonTaskRow.lease_expires_at <= now)
                    )
                ),
            )
            .order_by(PersonTaskRow.created_at, PersonTaskRow.row_index)
            .limit(1)
        )
        if not candidate:
            return None
        job = self._lock_job(session, candidate.job_id)
        if not job or job.status == JobStatus.cancelled.value:
            return None
        rows = session.execute(
            update(PersonTaskRow)
            .where(
                PersonTaskRow.person_id == candidate.person_id,
                PersonTaskRow.attempts == candidate.attempts,
                PersonTaskRow.attempts < self.settings.WORKER_MAX_ATTEMPTS,
                (
                    (PersonTaskRow.status == PersonStatus.queued.value)
                    | (
                        (PersonTaskRow.status == PersonStatus.researching.value)
                        & (PersonTaskRow.lease_expires_at <= now)
                    )
                ),
            )
            .values(
                status=PersonStatus.researching.value,
                attempts=PersonTaskRow.attempts + 1,
                lease_token=token,
                leased_by=worker_id,
                lease_expires_at=expires,
                started_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if rows != 1:
            return None
        session.refresh(candidate)
        self._refresh_job_status(session, job)
        return candidate

    def _mark_claimed(
        self,
        session: Session,
        job: JobRow,
        task: PersonTaskRow,
        worker_id: str,
        token: str,
        now: datetime,
        expires: datetime,
    ) -> None:
        task.status = PersonStatus.researching.value
        task.attempts += 1
        task.lease_token = token
        task.leased_by = worker_id
        task.lease_expires_at = expires
        task.started_at = task.started_at or now
        self._refresh_job_status(session, job)

    def _lock_job(self, session: Session, job_id: str) -> JobRow | None:
        if self.engine.dialect.name == "sqlite":
            # SQLite has one writer. Acquire its write lock before reading decisions
            # so cancellation/completion cannot race a read-only fence check.
            session.execute(update(JobRow).where(JobRow.job_id == job_id).values(status=JobRow.status))
        query = select(JobRow).where(JobRow.job_id == job_id)
        if self.engine.dialect.name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _locked_task_for_completion(self, session: Session, lease: Lease) -> PersonTaskRow | None:
        query = select(PersonTaskRow).where(
            PersonTaskRow.person_id == lease.person_id,
            PersonTaskRow.job_id == lease.job_id,
            PersonTaskRow.status == PersonStatus.researching.value,
            PersonTaskRow.lease_token == lease.token,
            PersonTaskRow.lease_expires_at > utcnow(),
        )
        if self.engine.dialect.name == "postgresql":
            query = query.with_for_update()
        return session.scalar(query)

    def _replace_result(
        self, session: Session, job_id: str, person_id: str, result: ResearchResult, now: datetime
    ) -> None:
        session.execute(delete(FieldDecisionRow).where(FieldDecisionRow.person_id == person_id))
        session.execute(delete(ProfileRow).where(ProfileRow.person_id == person_id))
        # Evidence and usage form an append/update ledger across worker attempts.
        # Reclaiming a lease must not erase already-paid work or its provenance.

        profile_payload = result.profile.model_dump(mode="json")
        session.add(
            ProfileRow(
                person_id=person_id,
                job_id=job_id,
                status=result.profile.status.value,
                profile_confidence=result.profile.profile_confidence,
                coverage=result.profile.coverage,
                review_required=result.profile.review_required,
                data_json=profile_payload,
                created_at=now,
            )
        )
        for field, decision in result.profile.fields.items():
            session.add(
                FieldDecisionRow(
                    job_id=job_id,
                    person_id=person_id,
                    field=field.value,
                    data_json=decision.model_dump(mode="json"),
                    created_at=now,
                )
            )
        for source in result.sources:
            session.merge(
                SourceRow(
                    source_id=source.source_id,
                    job_id=job_id,
                    person_id=person_id,
                    data_json=source.model_dump(mode="json"),
                    created_at=now,
                )
            )
        session.flush()
        for claim in result.claims:
            session.merge(
                EvidenceClaimRow(
                    claim_id=claim.claim_id,
                    job_id=job_id,
                    person_id=person_id,
                    source_id=claim.source_id,
                    field=claim.field.value,
                    data_json=claim.model_dump(mode="json"),
                    created_at=now,
                )
            )
        for usage in result.usage:
            session.merge(
                UsageRecordRow(
                    usage_id=usage.usage_id,
                    job_id=job_id,
                    person_id=person_id,
                    source_id=usage.source_id,
                    role=usage.role,
                    model=usage.model,
                    prompt_version=usage.prompt_version,
                    success=usage.success,
                    data_json=usage.model_dump(mode="json"),
                    created_at=now,
                )
            )

    def _person_result_view(self, session: Session, task: PersonTaskRow) -> PersonResultView:
        profile = session.get(ProfileRow, task.person_id)
        result = None
        if profile:
            sources = session.scalars(
                select(SourceRow).where(SourceRow.person_id == task.person_id).order_by(SourceRow.created_at)
            ).all()
            claims = session.scalars(
                select(EvidenceClaimRow)
                .where(EvidenceClaimRow.person_id == task.person_id)
                .order_by(EvidenceClaimRow.created_at)
            ).all()
            usage = session.scalars(
                select(UsageRecordRow)
                .where(UsageRecordRow.person_id == task.person_id)
                .order_by(UsageRecordRow.created_at)
            ).all()
            result = ResearchResult(
                profile=PersonProfile.model_validate(profile.data_json),
                sources=[SourceRecord.model_validate(row.data_json) for row in sources],
                claims=[EvidenceClaim.model_validate(row.data_json) for row in claims],
                usage=[UsageRecord.model_validate(row.data_json) for row in usage],
            )
            result.profile.status = PersonStatus(task.status)
            if task.status in {"queued", "researching", "failed", "cancelled"}:
                result.profile.research_status = task.status
        return PersonResultView(
            person_id=task.person_id,
            row_index=task.row_index,
            original_row=dict(task.original_row_json),
            status=PersonStatus(task.status),
            error_code=task.error_code,
            result=result,
        )

    def _heartbeat(self, session: Session, worker_id: str, now: datetime) -> None:
        if self.engine.dialect.name == "postgresql":
            stmt = pg_insert(WorkerHeartbeatRow).values(worker_id=worker_id, last_seen_at=now)
            stmt = stmt.on_conflict_do_update(
                constraint="worker_heartbeats_pkey",
                set_={"last_seen_at": now},
            )
        else:
            stmt = sqlite_insert(WorkerHeartbeatRow).values(worker_id=worker_id, last_seen_at=now)
            stmt = stmt.on_conflict_do_update(
                index_elements=["worker_id"],
                set_={"last_seen_at": now},
            )
        session.execute(stmt)

    def _counts(self, session: Session, job_id: str) -> dict[str, int]:
        rows = session.execute(
            select(PersonTaskRow.status, func.count())
            .where(PersonTaskRow.job_id == job_id)
            .group_by(PersonTaskRow.status)
        ).all()
        counts = {status.value: 0 for status in PersonStatus}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts

    def _refresh_job_status(self, session: Session, job: JobRow) -> None:
        if job.status == JobStatus.cancelled.value:
            return
        session.flush()
        counts = self._counts(session, job.job_id)
        total = job.total_people
        terminal = sum(counts[status] for status in TERMINAL_STATUSES)
        successes = sum(counts[status] for status in SUCCESS_STATUSES)
        failures = counts[PersonStatus.failed.value]
        cancelled = counts[PersonStatus.cancelled.value]
        running = counts[PersonStatus.researching.value]
        now = utcnow()

        if total == 0:
            job.status = JobStatus.completed.value
            job.completed_at = now
        elif terminal == total:
            if successes == total:
                job.status = JobStatus.completed.value
            elif failures == total:
                job.status = JobStatus.failed.value
            elif cancelled == total:
                job.status = JobStatus.cancelled.value
            else:
                job.status = JobStatus.partial.value
            job.completed_at = job.completed_at or now
        elif running or terminal:
            job.status = JobStatus.running.value
            job.started_at = job.started_at or now
        else:
            job.status = JobStatus.queued.value
        if job.status in {"completed", "partial", "failed", "cancelled"}:
            session.execute(delete(RetrievalCacheRow).where(RetrievalCacheRow.job_id == job.job_id))


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _set_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
