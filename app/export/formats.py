from __future__ import annotations

import csv
import io
import re
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.schemas import JobResults, ProfileField

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def export_results(results: JobResults, columns: list[str], format: str) -> bytes:
    rows, output_columns = _flatten_results(results, columns)
    normalized = format.lower()
    if normalized == "csv":
        return _export_csv(rows, output_columns)
    if normalized == "xlsx":
        return _export_xlsx(rows, output_columns)
    raise ValueError("Unsupported export format")


def _flatten_results(results: JobResults, columns: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    original_columns = list(columns)
    enrichments = ["status", "error_code"]
    for field in ProfileField:
        enrichments.extend([field.value, f"{field.value}_confidence"])
    enrichments.extend(["profile_confidence", "coverage", "review_required", "source_urls"])
    used_columns = list(original_columns)
    output_columns = original_columns + [_unique_column(name, used_columns) for name in enrichments]
    enrichment_map = dict(zip(enrichments, output_columns[len(original_columns) :], strict=True))

    rows: list[dict[str, Any]] = []
    for person in results.people:
        row = {column: person.original_row.get(column, "") for column in original_columns}
        row[enrichment_map["status"]] = person.status.value
        row[enrichment_map["error_code"]] = person.error_code or ""
        if person.result:
            profile = person.result.profile
            for field in ProfileField:
                decision = profile.fields.get(field)
                row[enrichment_map[field.value]] = (
                    decision.value if decision and decision.value is not None else ""
                )
                row[enrichment_map[f"{field.value}_confidence"]] = decision.confidence if decision else 0
            row[enrichment_map["profile_confidence"]] = profile.profile_confidence
            row[enrichment_map["coverage"]] = profile.coverage
            row[enrichment_map["review_required"]] = profile.review_required
            row[enrichment_map["source_urls"]] = _compact_source_urls(person.result)
        else:
            for field in ProfileField:
                row[enrichment_map[field.value]] = ""
                row[enrichment_map[f"{field.value}_confidence"]] = ""
            row[enrichment_map["profile_confidence"]] = ""
            row[enrichment_map["coverage"]] = ""
            row[enrichment_map["review_required"]] = ""
            row[enrichment_map["source_urls"]] = ""
        rows.append(row)
    return rows, output_columns


def _unique_column(name: str, existing: list[str]) -> str:
    seen = set(existing)
    candidate = name
    if candidate not in seen:
        existing.append(candidate)
        return candidate
    index = 2
    while True:
        candidate = f"{name} ({index})"
        if candidate not in seen:
            existing.append(candidate)
            return candidate
        index += 1


def _compact_source_urls(result) -> str:
    urls: list[str] = []
    for source in result.sources:
        url = source.canonical_url or source.final_url or source.requested_url
        if url and url not in urls:
            urls.append(url)
    return "; ".join(urls[:10])


def _safe_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _export_csv(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writerow({column: _safe_cell(column) for column in columns})
    for row in rows:
        writer.writerow({column: _safe_cell(row.get(column, "")) for column in columns})
    return output.getvalue().encode("utf-8-sig")


def _export_xlsx(rows: list[dict[str, Any]], columns: list[str]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Results"
    header_fill = PatternFill(fill_type="solid", fgColor="D9EAF7")
    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=_xlsx_cell(column))
        cell.font = Font(bold=True)
        cell.fill = header_fill
    for row_index, row in enumerate(rows, start=2):
        for column_index, column in enumerate(columns, start=1):
            sheet.cell(row=row_index, column=column_index, value=_xlsx_cell(row.get(column, "")))
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, column in enumerate(columns, start=1):
        width = max([len(str(column)), *[len(str(row.get(column, ""))) for row in rows]])
        sheet.column_dimensions[get_column_letter(index)].width = min(max(width + 2, 12), 60)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _xlsx_cell(value: Any) -> Any:
    if isinstance(value, str):
        # XML 1.0 cannot represent these control characters. Preserve other text,
        # replacing only invalid bytes with a visible replacement character.
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "\ufffd", value)
    return _safe_cell(value)
