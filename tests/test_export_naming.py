from __future__ import annotations

import csv
import io
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest

from app.export.formats import export_results
from app.export.naming import (
    MAX_LIST_NAME_BYTES,
    MAX_LIST_NAME_CHARS,
    content_disposition,
    export_filename,
    safe_list_name,
    source_list_name,
)
from app.schemas import JobResults, JobView, PersonResultView

JOB_ID = "d85054fd-f00a-4596-8981-bf003de3ca4c"


def _results(rows):
    return JobResults(
        job_id=JOB_ID,
        status="completed",
        people=[
            PersonResultView(
                person_id=f"person-{index}", row_index=index, original_row=row, status="completed"
            )
            for index, row in enumerate(rows)
        ],
    )


def _job(**overrides):
    values = {
        "job_id": JOB_ID,
        "status": "completed",
        "total_people": 2,
        "counts": {"completed": 2},
        "created_at": datetime(2026, 9, 28, 23, 1, tzinfo=timezone.utc),
        "started_at": datetime(2026, 9, 29, 2, 41, tzinfo=timezone.utc),
        "completed_at": datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return JobView(**values)


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_single_list_filename_uses_research_start_and_never_job_id(format):
    results = _results([{"name": "Jane Doe", "LIST_NAME": "Forbes 2000"}])
    assert export_filename(results, _job(), format) == f"Forbes-2000_2026-09-29_0241.{format}"
    assert JOB_ID not in export_filename(results, _job(), format)


def test_first_nonempty_list_uses_original_row_order_and_leaves_rows_unchanged():
    results = _results(
        [
            {"name": "One", "LIST_NAME": "  "},
            {"name": "Two", "LIST_NAME": "Forbes 2000"},
            {"name": "Three", "LIST_NAME": "YALI"},
        ]
    )
    results.people.reverse()
    original = deepcopy(results.model_dump())
    before = export_results(results, ["name", "LIST_NAME"], "csv")
    assert export_filename(results, _job(), "xlsx") == "Forbes-2000_2026-09-29_0241.xlsx"
    assert results.model_dump() == original
    assert export_results(results, ["name", "LIST_NAME"], "csv") == before
    rows = list(csv.DictReader(io.StringIO(before.decode("utf-8-sig"))))
    assert [row["LIST_NAME"] for row in rows] == ["YALI", "Forbes 2000", "  "]


@pytest.mark.parametrize(
    "header",
    ["LIST_NAME", "List Name", "source_list", "source_dataset", "dataset_name", "cohort", "source_cohort"],
)
def test_supported_passive_source_list_headers(header):
    results = _results([{header: "Prime Ministers"}])
    assert source_list_name(results) == "Prime Ministers"
    assert export_filename(results, _job(), "csv") == "Prime-Ministers_2026-09-29_0241.csv"


@pytest.mark.parametrize(
    "rows", [[], [{"name": "Jane", "company": "Acme"}], [{"LIST_NAME": None}], [{"LIST_NAME": " "}]]
)
def test_missing_source_list_uses_knownby_fallback(rows):
    assert export_filename(_results(rows), _job(), "xlsx") == "KnownBy_2026-09-29_0241.xlsx"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('  US  Government / Top\\500:*?"<>| Leaders  ', "US-Government-Top-500-Leaders"),
        ("Prime\t\n Ministers__---2026", "Prime-Ministers-2026"),
        ("../..\\\x00\u202e", "KnownBy"),
        ("Crème brûlée — 政府", "Crème-brûlée-政府"),
        ("CON", "CON"),
    ],
)
def test_safe_readable_list_name(value, expected):
    assert safe_list_name(value) == expected


@pytest.mark.parametrize("value", ["A" * 500, "政府" * 500])
def test_list_name_length_is_bounded_in_characters_and_utf8_bytes(value):
    slug = safe_list_name(value)
    filename = export_filename(_results([{"LIST_NAME": value}]), _job(), "xlsx")
    assert 0 < len(slug) <= MAX_LIST_NAME_CHARS
    assert len(slug.encode("utf-8")) <= MAX_LIST_NAME_BYTES
    assert len(filename.encode("utf-8")) < 255


def test_export_timestamp_is_stable_and_uses_utc():
    results = _results([{"LIST_NAME": "Forbes 2000"}])
    job = _job(started_at=datetime(2026, 9, 29, 8, 11, tzinfo=timezone(timedelta(hours=5, minutes=30))))
    first = export_filename(results, job, "csv")
    job.completed_at += timedelta(days=2)
    assert export_filename(results, job, "csv") == first == "Forbes-2000_2026-09-29_0241.csv"


def test_unstarted_job_has_stable_creation_time_fallback():
    assert export_filename(_results([]), _job(started_at=None), "csv") == "KnownBy_2026-09-28_2301.csv"


def test_filename_rejects_unsupported_format():
    with pytest.raises(ValueError, match="Unsupported export format"):
        export_filename(_results([]), _job(), "html")


def test_ascii_content_disposition_uses_human_readable_name():
    filename = "Forbes-2000_2026-09-29_0241.xlsx"
    assert content_disposition(filename) == f'attachment; filename="{filename}"'


def test_unicode_content_disposition_retains_filename_and_is_ascii_header_safe():
    filename = export_filename(_results([{"LIST_NAME": "政府 Crème"}]), _job(), "xlsx")
    header = content_disposition(filename)
    assert header.isascii()
    assert unquote(header.split("filename*=UTF-8''")[1]) == filename
    assert JOB_ID not in header


def test_unicode_compatibility_characters_cannot_inject_ascii_header_syntax():
    filename = "Safe＂；file.xlsx"
    header = content_disposition(filename)
    assert header.startswith('attachment; filename="Safe--file.xlsx";')
    assert unquote(header.split("filename*=UTF-8''")[1]) == filename


@pytest.mark.parametrize(
    "filename", ['bad".csv', "bad\r\nHeader: value.csv", "../file.csv", "a\\file.xlsx", ""]
)
def test_content_disposition_rejects_unsafe_names(filename):
    with pytest.raises(ValueError, match="Unsafe export filename"):
        content_disposition(filename)
