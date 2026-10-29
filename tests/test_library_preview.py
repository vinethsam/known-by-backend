"""Real offline save/list/preview/download/delete lifecycle and parser bounds."""

import csv
import io
from dataclasses import replace
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.db.models import JobRow, PersonTaskRow
from app.export.formats import ExportCellLimitError
from app.export.schema import RESEARCH_FIELDS, standard_columns
from app.library_preview import LibraryPreviewError, preview_saved_file
from tests.test_result_library import ADMIN_AUTH, AUTH, _artifact, _completed_job
from tests.test_result_library import library_context as library_context


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_saved_records_lifecycle_is_shared_paginated_and_unchanged(library_context, format):
    _, store, _, app = library_context()
    job_id = _completed_job(store)
    with TestClient(app) as client:
        saved = client.post(f"/v1/jobs/{job_id}/library", json={"format": format}, headers=AUTH).json()
        path = f"/v1/library/files/{saved['file_id']}"
        original = client.get(path, headers=AUTH).content
        with TestClient(app) as colleague:
            assert colleague.get("/v1/library/files", headers=AUTH).json() == [saved]
            response = colleague.get(f"{path}/records?limit=1", headers=AUTH)
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["total"] == 2 and page["has_more"]
            assert page["file"] == saved
            assert len(page["records"]) == 1
            first = page["records"][0]
            assert tuple(first) == standard_columns()
            assert first["full_name"] == "Jane Doe"
            assert first["job_title"] == "Programme Director"
            assert float(first["job_title_confidence"]) == 40
            assert str(first["review_required"]).lower() == "true"
            assert "https://employer.example.org/profile" in first["job_title_source_urls"]
            assert response.headers["cache-control"] == "private, no-store"
            second = colleague.get(f"{path}/records?limit=1&offset=1", headers=AUTH).json()
            assert second["records"][0]["full_name"] == "John Doe" and not second["has_more"]
            filtered = colleague.get(f"{path}/records?q=john", headers=AUTH).json()
            assert filtered["total"] == 1 and filtered["records"][0]["full_name"] == "John Doe"
            assert colleague.get(f"{path}/records?q=absent", headers=AUTH).json()["records"] == []
            assert colleague.get(path, headers=AUTH).content == original
            assert colleague.delete(path, headers=AUTH).status_code == 403
            assert colleague.delete(path, headers=ADMIN_AUTH).status_code == 204
        assert client.get(f"{path}/records", headers=AUTH).status_code == 404


def test_finished_job_with_failures_can_save_available_results(library_context):
    _, store, _, app = library_context()
    job_id = _completed_job(store)
    with store.session_factory.begin() as session:
        job = session.get(JobRow, job_id)
        job.status = "partial"
        person = session.query(PersonTaskRow).filter_by(job_id=job_id).first()
        person.status = "failed"
        person.error_code = "IDENTITY_UNRESOLVED"
    with TestClient(app) as client:
        response = client.post(f"/v1/jobs/{job_id}/library", json={"format": "csv"}, headers=AUTH)
        assert response.status_code == 201, response.text
        page = client.get(response.headers["location"] + "/records", headers=AUTH).json()
        assert page["total"] == 2


def test_partial_job_without_successful_result_cannot_be_saved(library_context):
    _, store, _, app = library_context()
    job_id = _completed_job(store)
    with store.session_factory.begin() as session:
        session.get(JobRow, job_id).status = "partial"
        for person in session.query(PersonTaskRow).filter_by(job_id=job_id):
            person.status = "failed"
    with TestClient(app) as client:
        response = client.post(f"/v1/jobs/{job_id}/library", json={"format": "csv"}, headers=AUTH)
        assert response.status_code == 409


def test_export_cell_limit_is_an_actionable_error_and_never_saves_partial_file(library_context, monkeypatch):
    _, store, storage, app = library_context()
    job_id = _completed_job(store)

    def oversized(*args, **kwargs):
        raise ExportCellLimitError(
            "A result cell exceeds the spreadsheet limit; supplied values remain in record details."
        )

    monkeypatch.setattr("app.api.routes.build_export_artifact", oversized)
    with TestClient(app) as client:
        for format in ("csv", "xlsx"):
            exported = client.get(f"/v1/jobs/{job_id}/export?format={format}", headers=AUTH)
            saved = client.post(f"/v1/jobs/{job_id}/library", json={"format": format}, headers=AUTH)
            assert exported.status_code == saved.status_code == 422
            assert "record details" in exported.json()["detail"]
        assert storage.list_files() == []


@pytest.mark.parametrize("params", ["limit=101", "limit=0", "offset=-1", "q=" + "x" * 201])
def test_preview_rejects_unbounded_parameters(library_context, params):
    _, store, storage, app = library_context()
    saved = storage.save(_artifact(store, _completed_job(store)))
    with TestClient(app) as client:
        assert (
            client.get(f"/v1/library/files/{saved.file_id}/records?{params}", headers=AUTH).status_code == 422
        )


def test_legacy_csv_recognizes_friendly_headers_and_ignores_admin_columns(library_context):
    _, store, storage, app = library_context()
    artifact = _artifact(store, _completed_job(store))
    content = b"Full Name,Organisation,Job Title,Review Required,Source URLs,LIST_NAME\r\njane doe,IBM,chief executive officer,True,https://example.org/jane,Private Admin\r\n"
    saved = storage.save(replace(artifact, content=content))
    with TestClient(app) as client:
        path = f"/v1/library/files/{saved.file_id}"
        page = client.get(path + "/records", headers=AUTH).json()
        record = page["records"][0]
        assert record["full_name"] == "Jane Doe"
        assert record["organisation"] == "IBM"
        assert record["job_title"] == "Chief Executive Officer"
        assert "LIST_NAME" not in record
        assert client.get(path, headers=AUTH).content == content


@pytest.mark.parametrize("format", ["csv", "xlsx"])
@pytest.mark.parametrize("provenance", [False, True])
def test_legacy_generated_export_ignores_duplicate_admin_status(library_context, format, provenance):
    _, store, storage, _ = library_context()
    artifact = _artifact(store, _completed_job(store), format)
    fields = [field.value for field in RESEARCH_FIELDS]
    header = [
        *fields,
        *(f"{field}_confidence" for field in fields),
        "profile_confidence",
        "coverage",
        "legacy_coverage",
        "review_required",
        "status",
        "error_code",
        "source_urls",
    ]
    if provenance:
        header.extend(
            [
                *(f"{field}_source_urls" for field in fields),
                *(
                    f"{field}_{detail}"
                    for field in fields
                    for detail in (
                        "supplied_values",
                        "input_confidence",
                        "research_confidence",
                        "provenance_status",
                    )
                ),
                "seed_input_metadata",
            ]
        )
    header.extend(["STATUS", "LIST_NAME", "full_name (2)"])
    values = {
        "full_name": "Jane Doe",
        "full_name_confidence": 100,
        "full_name_input_confidence": 100,
        "full_name_research_confidence": 0,
        "full_name_provenance_status": "supplied",
        "status": "review_required",
        "review_required": True,
        "source_urls": "https://official.example.org/jane",
        "STATUS": "staged",
        "LIST_NAME": "Private Input",
        "full_name (2)": "Wrong Name",
    }
    row = [values.get(key, "") for key in header]
    if format == "csv":
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerows([header, row])
        content = stream.getvalue().encode()
    else:
        workbook = Workbook()
        workbook.active.append(header)
        workbook.active.append(row)
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        content = buffer.getvalue()
    saved = storage.save(replace(artifact, content=content))
    retained = storage.get(saved.file_id)
    record = preview_saved_file(retained).records[0]
    assert record["full_name"] == "Jane Doe"
    assert record["status"] == "review_required"
    assert "LIST_NAME" not in record
    assert float(record["full_name_confidence"]) == (0 if provenance else 100)
    assert retained.content == content


@pytest.mark.parametrize(
    "content", [b"broken header\nno research", b"full_name,full_name\nJane,John", b'full_name\n"unterminated']
)
def test_malformed_preview_does_not_break_original_download(library_context, content):
    _, store, storage, app = library_context()
    saved = storage.save(replace(_artifact(store, _completed_job(store)), content=content))
    with TestClient(app) as client:
        path = f"/v1/library/files/{saved.file_id}"
        assert client.get(path + "/records", headers=AUTH).status_code == 422
        assert client.get(path, headers=AUTH).content == content


def test_xlsx_formulas_are_inert_and_zip_expansion_is_bounded(library_context):
    _, store, storage, _ = library_context()
    artifact = _artifact(store, _completed_job(store), "xlsx")
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["full_name", "organisation"])
    sheet.append(["Jane Doe", '=HYPERLINK("https://untrusted.invalid", "run")'])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    saved = storage.save(replace(artifact, content=buffer.getvalue()))
    retained = storage.get(saved.file_id)
    page = preview_saved_file(retained)
    assert page.records[0]["organisation"].startswith("=HYPERLINK(")
    with pytest.raises(LibraryPreviewError, match="too large"):
        preview_saved_file(retained, max_uncompressed_bytes=1)
    with pytest.raises(LibraryPreviewError, match="too large"):
        preview_saved_file(retained, max_file_bytes=1)

    corrupted = io.BytesIO()
    with ZipFile(io.BytesIO(buffer.getvalue())) as source, ZipFile(corrupted, "w") as target:
        for entry in source.infolist():
            target.writestr(
                entry, b"<broken" if entry.filename == "xl/worksheets/sheet1.xml" else source.read(entry)
            )
    with pytest.raises(LibraryPreviewError, match="cannot be previewed"):
        preview_saved_file(replace(retained, content=corrupted.getvalue()))


def test_large_csv_keeps_only_requested_page(library_context):
    _, store, storage, _ = library_context()
    artifact = _artifact(store, _completed_job(store))
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["full_name", "job_title"])
    writer.writerows((f"Person {index}", "Analyst") for index in range(5000))
    saved = storage.save(replace(artifact, content=stream.getvalue().encode()))
    page = preview_saved_file(storage.get(saved.file_id), offset=4990, limit=10)
    assert page.total == 5000 and len(page.records) == 10 and not page.has_more
