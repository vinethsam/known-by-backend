"""Retained exports use real API, export, storage, and migration paths offline."""

from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import delete, event, func, select

from alembic import command
from app.api.routes import create_app
from app.config import Settings
from app.db import Store
from app.db.library import DatabaseResultFileStorage
from app.db.models import JobRow, PersonTaskRow, SavedResultFileRow, SourceRow, UsageRecordRow
from app.export.artifacts import build_export_artifact
from app.library import LibraryLimitError
from app.schemas import (
    EvidenceClaim,
    FieldDecision,
    IdentityMatch,
    PersonProfile,
    PersonSeed,
    ProfileField,
    ResearchResult,
    SourceRecord,
    SourceType,
)

AUTH = {"Authorization": "Bearer shared-test-token"}
STARTED = datetime(2026, 9, 29, 2, 41, tzinfo=timezone.utc)


class _UnusedFetcher:
    async def fetch(self, *args, **kwargs):
        raise AssertionError("Library operations must never retrieve research pages")

    async def close(self):
        pass


@pytest.fixture
def library_context(tmp_path):
    stores = []

    def create(**overrides):
        values = {
            "APP_ENV": "test",
            "DATABASE_URL": f"sqlite:///{tmp_path / f'library-{len(stores)}.db'}",
            "API_ACCESS_TOKEN": "shared-test-token",
            "PLAYWRIGHT_ENABLED": False,
        }
        values.update(overrides)
        settings = Settings(_env_file=None, **values)
        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
        command.upgrade(cfg, "head")
        store = Store(settings)
        stores.append(store)
        storage = DatabaseResultFileStorage(store.session_factory, settings)
        app = create_app(settings, store, fetcher=_UnusedFetcher(), library=storage)
        return settings, store, storage, app

    yield create
    for store in stores:
        store.engine.dispose()


def _completed_job(store, *, list_name="Forbes 2000"):
    rows = [
        {"name": "Jane Doe", "LIST_NAME": list_name},
        {"name": "John Doe", "LIST_NAME": "YALI"},
    ]
    created = store.create_job(
        [PersonSeed(full_name=row["name"]) for row in rows], rows, ["name", "LIST_NAME"]
    )
    for _ in rows:
        lease = store.claim_task("library-fixture")
        assert lease is not None
        fields = {
            ProfileField.full_name: FieldDecision(
                value=lease.seed.full_name, confidence=100, review_required=False
            ),
            ProfileField.job_title: FieldDecision(
                value="Programme Director",
                confidence=40,
                review_required=True,
                sources=["https://employer.example.org/profile"],
            ),
        }
        result = ResearchResult(
            profile=PersonProfile(
                person_id=lease.person_id,
                input_name=lease.seed.full_name,
                status="review_required",
                fields=fields,
                profile_confidence=40,
                coverage=15,
                review_required=True,
            )
        )
        assert store.finish_task(lease, result)
    with store.session_factory.begin() as session:
        job = session.get(JobRow, created.job_id)
        job.started_at = STARTED
    assert store.get_job(created.job_id).status == "completed"
    return created.job_id


def _artifact(store, job_id, format="csv"):
    return build_export_artifact(
        store.get_results(job_id), store.get_columns(job_id), store.get_job(job_id), format
    )


def _workbook_content(content):
    workbook = load_workbook(io.BytesIO(content))
    try:
        return [
            (
                sheet.title,
                sheet.freeze_panes,
                sheet.auto_filter.ref,
                tuple(
                    tuple((cell.value, cell.data_type, tuple(cell._style)) for cell in row) for row in sheet
                ),
            )
            for sheet in workbook
        ]
    finally:
        workbook.close()


@pytest.mark.parametrize("format", ["csv", "xlsx"])
@pytest.mark.parametrize("provenance", ["none", "field"])
def test_explicit_save_matches_normal_export_with_same_human_filename(library_context, format, provenance):
    _, store, storage, app = library_context()
    job_id = _completed_job(store)
    expected_name = f"Forbes-2000_2026-09-29_0241.{format}"
    with TestClient(app) as client:
        assert client.get("/v1/library/files", headers=AUTH).json() == []
        normal = client.get(
            f"/v1/jobs/{job_id}/export", params={"format": format, "provenance": provenance}, headers=AUTH
        )
        assert normal.status_code == 200
        # Completion and ordinary download are both intentionally non-retaining.
        assert storage.list_files() == []
        saved = client.post(
            f"/v1/jobs/{job_id}/library", json={"format": format, "provenance": provenance}, headers=AUTH
        )
        assert saved.status_code == 201, saved.text
        metadata = saved.json()
        assert metadata["filename"] == expected_name
        assert job_id not in metadata["filename"]
        assert metadata["list_name"] == "Forbes 2000"
        assert metadata["research_started_at"] == "2026-09-29T02:41:00Z"
        assert metadata["format"] == format
        assert saved.headers["location"] == f"/v1/library/files/{metadata['file_id']}"
        download = client.get(saved.headers["location"], headers=AUTH)
        assert download.status_code == 200
        assert download.headers["content-disposition"] == normal.headers["content-disposition"]
        assert expected_name in download.headers["content-disposition"]
        assert download.headers["cache-control"] == "private, no-store"
        assert download.headers["x-content-type-options"] == "nosniff"
        assert metadata["size_bytes"] == len(download.content)
        if format == "csv":
            assert download.content == normal.content
            assert "Forbes 2000" in download.text and "YALI" in download.text
        else:
            assert _workbook_content(download.content) == _workbook_content(normal.content)
        assert client.get("/v1/library/files", headers=AUTH).json() == [metadata]


def test_authorized_team_members_share_list_download_and_delete(library_context):
    _, store, _, app = library_context()
    job_id = _completed_job(store)
    with TestClient(app) as creator:
        saved = creator.post(f"/v1/jobs/{job_id}/library", json={"format": "csv"}, headers=AUTH).json()
        path = f"/v1/library/files/{saved['file_id']}"
        with TestClient(app) as teammate:
            assert teammate.get("/v1/library/files", headers=AUTH).json() == [saved]
            assert teammate.get(path, headers=AUTH).status_code == 200
            assert teammate.delete(path, headers=AUTH).status_code == 204
        assert creator.get(path, headers=AUTH).status_code == 404
        assert creator.get("/v1/library/files", headers=AUTH).json() == []
        assert creator.delete(path, headers=AUTH).status_code == 404


def test_legacy_social_evidence_cannot_reenter_results_exports_or_saved_library(library_context):
    _, store, _, app = library_context()
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    lease = store.claim_task("legacy-fixture")
    social_url = "https://www.linkedin.com/in/jane-doe"
    source = SourceRecord(
        person_id=lease.person_id,
        requested_url=social_url,
        final_url=social_url,
        canonical_url=social_url,
        domain="www.linkedin.com",
        source_type=SourceType.employer,
        authority_score=1,
        identity=IdentityMatch(score=1, ambiguous=False),
    )
    claim = EvidenceClaim(
        person_id=lease.person_id,
        source_id=source.source_id,
        field=ProfileField.organisation,
        raw_value="Unverified Social Org",
        normalised_value="unverified social org",
        evidence_text="Jane Doe works at Unverified Social Org",
        subject_name="Jane Doe",
        identity_relevance=1,
        extraction_model="legacy-fixture",
    )
    result = ResearchResult(
        profile=PersonProfile(
            person_id=lease.person_id,
            input_name="Jane Doe",
            status="completed",
            profile_confidence=99,
            coverage=100,
            review_required=False,
            fields={
                ProfileField.organisation: FieldDecision(
                    value=claim.raw_value,
                    confidence=99,
                    sources=[social_url],
                    selected_claim_id=claim.claim_id,
                    supporting_claim_ids=[claim.claim_id],
                ),
                ProfileField.profile_link: FieldDecision(value=social_url, confidence=99),
            },
        ),
        sources=[source],
        claims=[claim],
    )
    assert store.finish_task(lease, result)
    with TestClient(app) as client:
        response = client.get(f"/v1/jobs/{job.job_id}/results", headers=AUTH)
        assert response.status_code == 200
        sanitized = response.json()["people"][0]["result"]
        assert sanitized["sources"] == sanitized["claims"] == []
        assert sanitized["profile"]["fields"]["organisation"]["value"] is None
        assert sanitized["profile"]["fields"]["profile_link"]["value"] is None
        assert sanitized["profile"]["profile_confidence"] == 0
        normal = client.get(f"/v1/jobs/{job.job_id}/export?format=csv&provenance=field", headers=AUTH)
        saved = client.post(
            f"/v1/jobs/{job.job_id}/library",
            json={"format": "csv", "provenance": "field"},
            headers=AUTH,
        )
        assert saved.status_code == 201
        retained = client.get(saved.headers["location"], headers=AUTH)
        assert retained.content == normal.content
        assert "linkedin.com" not in retained.text
        assert "Unverified Social Org" not in retained.text
    # Read-time policy enforcement does not rewrite the historical source ledger.
    with store.session_factory() as session:
        assert session.get(SourceRow, source.source_id).data_json["canonical_url"] == social_url


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}])
def test_unauthorized_requests_cannot_view_download_create_or_delete_saved_files(library_context, headers):
    _, store, _, app = library_context()
    job_id = _completed_job(store)
    with TestClient(app) as client:
        saved = client.post(f"/v1/jobs/{job_id}/library", json={"format": "csv"}, headers=AUTH).json()
        path = f"/v1/library/files/{saved['file_id']}"
        requests = [
            client.post(f"/v1/jobs/{job_id}/library", json={"format": "csv"}, headers=headers),
            client.get("/v1/library/files", headers=headers),
            client.get(path, headers=headers),
            client.delete(path, headers=headers),
        ]
        assert [response.status_code for response in requests] == [401] * 4
        assert all("Forbes" not in response.text and "Jane" not in response.text for response in requests)
        assert client.get(path, headers=AUTH).status_code == 200


@pytest.mark.parametrize("token", [None, "", " "])
def test_library_is_disabled_when_shared_authentication_is_unconfigured(library_context, token):
    _, _, _, app = library_context(API_ACCESS_TOKEN=token)
    identifier = uuid4()
    with TestClient(app) as client:
        responses = [
            client.post(f"/v1/jobs/{identifier}/library", json={"format": "csv"}),
            client.get("/v1/library/files"),
            client.get(f"/v1/library/files/{identifier}"),
            client.delete(f"/v1/library/files/{identifier}"),
        ]
        assert [response.status_code for response in responses] == [503] * 4
        assert all("API_ACCESS_TOKEN" in response.text for response in responses)


@pytest.mark.parametrize("format", ["pdf", "html", "xls", "json"])
def test_unsupported_library_formats_are_rejected(library_context, format):
    _, store, storage, app = library_context()
    job_id = _completed_job(store)
    with TestClient(app) as client:
        response = client.post(f"/v1/jobs/{job_id}/library", json={"format": format}, headers=AUTH)
        assert response.status_code == 422
    assert storage.list_files() == []


def test_library_does_not_accept_arbitrary_uploaded_content_or_metadata(library_context):
    _, store, storage, app = library_context()
    job_id = _completed_job(store)
    with TestClient(app) as client:
        response = client.post(
            f"/v1/jobs/{job_id}/library",
            json={"format": "csv", "content": "not an export", "filename": "arbitrary.csv"},
            headers=AUTH,
        )
        assert response.status_code == 422
        assert (
            client.post(f"/v1/jobs/{uuid4()}/library", json={"format": "csv"}, headers=AUTH).status_code
            == 404
        )
    assert storage.list_files() == []


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "partial"])
def test_only_completed_jobs_can_be_saved(library_context, status):
    _, store, storage, app = library_context()
    job_id = _completed_job(store)
    with store.session_factory.begin() as session:
        session.get(JobRow, job_id).status = status
    with TestClient(app) as client:
        assert (
            client.post(f"/v1/jobs/{job_id}/library", json={"format": "csv"}, headers=AUTH).status_code == 409
        )
    assert storage.list_files() == []


def test_completed_status_without_persisted_result_cannot_create_file(library_context):
    _, store, storage, app = library_context()
    job = store.create_job([PersonSeed(full_name="Jane Doe")])
    with store.session_factory.begin() as session:
        session.get(JobRow, job.job_id).status = "completed"
    with TestClient(app) as client:
        response = client.post(f"/v1/jobs/{job.job_id}/library", json={"format": "csv"}, headers=AUTH)
        assert response.status_code == 409
    assert storage.list_files() == []


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_configured_file_size_limit_rejects_oversized_exports(library_context, format):
    _, store, storage, app = library_context(LIBRARY_MAX_FILE_BYTES=1)
    job_id = _completed_job(store)
    with TestClient(app) as client:
        response = client.post(f"/v1/jobs/{job_id}/library", json={"format": format}, headers=AUTH)
        assert response.status_code == 413
        assert "LIBRARY_MAX_FILE_BYTES" in response.text
    assert storage.list_files() == []


def test_library_survives_temporary_job_cleanup(library_context):
    _, store, storage, app = library_context()
    job_id = _completed_job(store)
    with TestClient(app) as client:
        saved = client.post(f"/v1/jobs/{job_id}/library", json={"format": "xlsx"}, headers=AUTH).json()
        path = f"/v1/library/files/{saved['file_id']}"
        content = client.get(path, headers=AUTH).content
        with store.session_factory.begin() as session:
            session.execute(delete(JobRow).where(JobRow.job_id == job_id))
        assert store.get_job(job_id) is None
        with store.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(PersonTaskRow)) == 0
        assert client.get(path, headers=AUTH).content == content
        assert client.get("/v1/library/files", headers=AUTH).json() == [saved]
        assert storage.get(saved["file_id"]).content == content


def test_saving_uses_no_provider_client_research_call_or_additional_usage(library_context, monkeypatch):
    _, store, _, app = library_context()
    job_id = _completed_job(store)

    def forbidden(*args, **kwargs):
        raise AssertionError("Saving must not initiate research or a paid provider call")

    monkeypatch.setattr("app.providers.openrouter.OpenRouterClient.__init__", forbidden)
    monkeypatch.setattr("app.providers.openrouter.OpenRouterClient.complete", forbidden)
    monkeypatch.setattr("app.research.orchestrator.ResearchOrchestrator.research", forbidden)
    with TestClient(app) as client:
        assert client.post(f"/v1/jobs/{job_id}/library", json={}, headers=AUTH).status_code == 201
    with store.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UsageRecordRow)) == 0


def test_library_listing_never_loads_binary_content_and_supports_pagination(library_context):
    _, store, storage, app = library_context()
    job_id = _completed_job(store)
    artifact = _artifact(store, job_id)
    saved = [storage.save(artifact), storage.save(artifact)]
    queries = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement.lower())

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        with TestClient(app) as client:
            first = client.get("/v1/library/files?limit=1", headers=AUTH).json()
            second = client.get("/v1/library/files?limit=1&offset=1", headers=AUTH).json()
            assert len(first) == len(second) == 1
            assert {first[0]["file_id"], second[0]["file_id"]} == {item.file_id for item in saved}
            assert "content" not in first[0]
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    reads = [query for query in queries if query.lstrip().startswith("select")]
    assert len(reads) == 2
    assert all("saved_result_files.content" not in query for query in reads)


@pytest.mark.parametrize("quota", ["files", "bytes"])
def test_concurrent_storage_instances_enforce_shared_quotas_and_delete_releases_space(library_context, quota):
    settings, store, storage, _ = library_context()
    artifact = _artifact(store, _completed_job(store))
    settings.LIBRARY_MAX_FILES = 1 if quota == "files" else 10
    settings.LIBRARY_MAX_TOTAL_BYTES = len(artifact.content) if quota == "bytes" else 1_000_000
    other_store = Store(settings)
    other_storage = DatabaseResultFileStorage(other_store.session_factory, settings)
    barrier = Barrier(2)

    def save_once(adapter):
        barrier.wait(timeout=5)
        try:
            return adapter.save(artifact)
        except LibraryLimitError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save_once, [storage, other_storage]))
        failures = [result for result in results if isinstance(result, LibraryLimitError)]
        assert len(failures) == 1
        assert len(storage.list_files()) == 1
        assert storage.delete(storage.list_files()[0].file_id)
        replacement = other_storage.save(artifact)
        assert [item.file_id for item in storage.list_files()] == [replacement.file_id]
        with store.session_factory() as session:
            assert session.scalar(select(func.sum(SavedResultFileRow.size_bytes))) == len(artifact.content)
    finally:
        other_store.engine.dispose()
