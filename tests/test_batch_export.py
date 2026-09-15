from __future__ import annotations

import csv
import io
import re
import zipfile

import pytest
from openpyxl import Workbook, load_workbook

from app.config import Settings
from app.export.batch import BatchParseError, parse_batch
from app.export.formats import export_results
from app.schemas import (
    FieldDecision,
    JobResults,
    PersonProfile,
    PersonResultView,
    PersonStatus,
    ProfileField,
    ResearchResult,
    SourceRecord,
)


def _settings(**overrides):
    data = {
        "APP_ENV": "test",
        "MAX_BATCH_ROWS": 2,
        "MAX_UPLOAD_BYTES": 20_000,
        "MAX_XLSX_UNCOMPRESSED_BYTES": 100_000,
        "MAX_INPUT_COLUMNS": 5,
    }
    data.update(overrides)
    return Settings(**data)


def test_parse_csv_preserves_rows_columns_and_row_indexes():
    content = b"name,organisation,note\nAda Lovelace,Analytical Engines,first\nGrace Hopper,Navy,second\n"
    batch = parse_batch(content, "people.csv", None, _settings())
    assert [seed.full_name for seed in batch.seeds] == ["Ada Lovelace", "Grace Hopper"]
    assert batch.columns == ["name", "organisation", "note"]
    assert batch.rows[0] == {
        "name": "Ada Lovelace",
        "organisation": "Analytical Engines",
        "note": "first",
    }
    assert batch.seeds[0].organisation == "Analytical Engines"
    assert batch.seeds[0].known_attributes == {}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"name,name\nAda,Again\n", "duplicate"),
        (b"name,\nAda,x\n", "blank"),
        (b"person\nAda\n", "missing"),
        (b"name,full_name\nAda,Ada Lovelace\n", "ambiguous"),
        (b"name\n\n", "Blank row"),
    ],
)
def test_parse_csv_rejects_bad_headers_and_missing_people(content, message):
    with pytest.raises(BatchParseError, match=message):
        parse_batch(content, "people.csv", None, _settings())


def test_parse_rejects_limits_and_missing_name():
    with pytest.raises(BatchParseError, match="too many rows"):
        parse_batch(b"name\nAda Lovelace\nGrace Hopper\nAlan Turing\n", "people.csv", None, _settings())
    with pytest.raises(BatchParseError, match="Missing name"):
        parse_batch(b"name\nAda Lovelace\n \n", "people.csv", None, _settings(MAX_BATCH_ROWS=5))
    with pytest.raises(BatchParseError, match="too many columns"):
        parse_batch(b"name,a,b,c,d,e\nAda,1,2,3,4,5\n", "people.csv", None, _settings())
    with pytest.raises(BatchParseError, match="maximum size"):
        parse_batch(b"name\n" + (b"a" * 2000), "people.csv", None, _settings(MAX_UPLOAD_BYTES=1024))


def test_parse_xlsx_preserves_formula_text_and_rejects_zipbomb(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["full name", "note"])
    sheet.append(["Ada Lovelace", "=1+1"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()

    batch = parse_batch(buffer.getvalue(), "people.xlsx", None, _settings())
    assert batch.rows[0]["note"] == "=1+1"

    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", "x" * 200_000)
    with pytest.raises(BatchParseError, match="maximum allowed size"):
        parse_batch(bomb.getvalue(), "people.xlsx", None, _settings(MAX_XLSX_UNCOMPRESSED_BYTES=1024))


def _results() -> JobResults:
    person_id = "person-1"
    fields = {
        field: FieldDecision(value=f"{field.value} value", confidence=88, review_required=False)
        for field in ProfileField
    }
    fields[ProfileField.full_name] = FieldDecision(value="=Ada", confidence=90, review_required=False)
    profile = PersonProfile(
        person_id=person_id,
        input_name="Ada Lovelace",
        status=PersonStatus.completed,
        fields=fields,
        profile_confidence=92,
        coverage=100,
        review_required=False,
    )
    source = SourceRecord(
        person_id=person_id,
        requested_url="https://example.com",
        final_url="https://example.com",
        canonical_url="https://example.com",
        domain="example.com",
    )
    return JobResults(
        job_id="job-1",
        status="completed",
        people=[
            PersonResultView(
                person_id=person_id,
                row_index=2,
                original_row={"name": "+Ada Lovelace", "status": "original"},
                status=PersonStatus.completed,
                result=ResearchResult(profile=profile, sources=[source], claims=[], usage=[]),
            )
        ],
    )


def test_export_csv_is_formula_safe_and_collision_safe():
    data = export_results(_results(), ["name", "status"], "csv")
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0]["name"] == "'+Ada Lovelace"
    assert rows[0]["status"] == "original"
    assert rows[0]["status (2)"] == "completed"
    assert rows[0]["full_name"] == "'=Ada"
    assert rows[0]["source_urls"] == "https://example.com"


def test_export_xlsx_is_readable_and_formula_safe():
    data = export_results(_results(), ["name", "status"], "xlsx")
    workbook = load_workbook(io.BytesIO(data), data_only=False)
    sheet = workbook.active
    headers = [cell.value for cell in sheet[1]]
    row = [cell.value for cell in sheet[2]]
    workbook.close()
    assert headers[:3] == ["name", "status", "status (2)"]
    assert row[0] == "'+Ada Lovelace"
    assert row[headers.index("full_name")] == "'=Ada"


def test_export_has_each_enrichment_once_and_keeps_input_columns_unchanged():
    columns = ["name", "status", "status (2)", "full_name"]
    rows = list(csv.reader(io.StringIO(export_results(_results(), columns, "csv").decode("utf-8-sig"))))
    assert len(rows[0]) == len(columns) + 20
    assert len(set(rows[0])) == len(rows[0])
    assert rows[0][:4] == columns
    assert "status (3)" in rows[0] and "full_name (2)" in rows[0]
    assert columns == ["name", "status", "status (2)", "full_name"]


@pytest.mark.parametrize("format", ["csv", "xlsx"])
def test_export_escapes_headers_and_whitespace_prefixed_formulas(format):
    results = _results()
    results.people[0].original_row = {" =header": ' \t=HYPERLINK("https://example.org")'}
    data = export_results(results, [" =header"], format)
    if format == "csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    else:
        workbook = load_workbook(io.BytesIO(data), data_only=False)
        rows = list(workbook.active.values)
        workbook.close()
    assert rows[0][0] == "' =header"
    assert rows[1][0].startswith("' \t=")


@pytest.mark.parametrize("hidden_cell", ["Z2", "A10000"])
def test_xlsx_cannot_hide_excess_cells_with_false_dimensions(hidden_cell):
    workbook = Workbook()
    workbook.active.append(["name"])
    workbook.active.append(["Jane Doe"])
    workbook.active[hidden_cell] = "unexpected"
    original = io.BytesIO()
    workbook.save(original)
    workbook.close()
    modified = io.BytesIO()
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(modified, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                content = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A2"', content)
            target.writestr(item.filename, content)
    with pytest.raises(BatchParseError, match="exceed configured"):
        parse_batch(modified.getvalue(), "people.xlsx", None, _settings())


def test_xlsx_replaces_xml_invalid_input_without_losing_other_text():
    results = _results()
    results.people[0].original_row["name"] = "Jane\x00 Doe"
    data = export_results(results, ["name"], "xlsx")
    workbook = load_workbook(io.BytesIO(data))
    assert workbook.active.cell(2, 1).value == "Jane\ufffd Doe"
    workbook.close()
