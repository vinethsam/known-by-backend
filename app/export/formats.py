from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Iterator
from typing import Any, Literal

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.config import DEFAULT_REVIEW_THRESHOLD
from app.export.schema import RESEARCH_FIELDS, provenance_columns, standard_columns
from app.research.normalisation import finalize_display
from app.retrieval.urls import source_policy_allows, source_record_allowed
from app.schemas import JobResults, ProfileField

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
ProvenanceMode = Literal["none", "field"]


def export_results(
    results: JobResults,
    columns: list[str],
    format: str,
    *,
    provenance: ProvenanceMode = "none",
    low_confidence_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> bytes:
    if provenance not in {"none", "field"}:
        raise ValueError("Unsupported provenance mode")
    rows, output_columns = _flatten_results(results, columns, provenance=provenance)
    normalized = format.lower()
    if normalized == "csv":
        return _export_csv(rows, output_columns)
    if normalized == "xlsx":
        return _export_xlsx(
            rows,
            output_columns,
            field_columns=_field_column_pairs(),
            low_confidence_threshold=low_confidence_threshold,
        )
    raise ValueError("Unsupported export format")


def _flatten_results(
    results: JobResults,
    columns: list[str],
    *,
    provenance: ProvenanceMode = "none",
) -> tuple[Iterator[dict[str, Any]], list[str]]:
    original_columns = list(columns)
    standard = list(standard_columns())
    if provenance == "field":
        standard.extend(provenance_columns())
    used_columns = list(standard)
    metadata_map = {name: _unique_column(name, used_columns) for name in original_columns}
    return _iter_rows(results, metadata_map, provenance=provenance), used_columns


def _iter_rows(
    results: JobResults,
    metadata_map: dict[str, str],
    *,
    provenance: ProvenanceMode,
) -> Iterator[dict[str, Any]]:
    for person in results.people:
        if person.result:
            profile = finalize_display(person.result.profile)
            # Historical profiles have no records. Treat their flat decision set as
            # one record with the same columns and final selected-value normalization.
            records = getattr(profile, "records", ()) or (profile,)
            source_urls = _compact_source_urls(person.result)
            for record in records:
                row = _base_row(person, metadata_map, status=record.status)
                for field in RESEARCH_FIELDS:
                    decision = record.fields.get(field)
                    if (
                        field == ProfileField.profile_link
                        and decision
                        and decision.value
                        and not source_policy_allows(decision.value)
                    ):
                        decision = None
                    row[field.value] = decision.value if decision and decision.value is not None else ""
                    row[f"{field.value}_confidence"] = decision.confidence if decision else 0
                    if provenance == "field":
                        row[f"{field.value}_source_urls"] = _compact_urls(
                            decision.sources if decision else ()
                        )
                row["profile_confidence"] = record.profile_confidence
                row["coverage"] = record.coverage
                row["legacy_coverage"] = record.legacy_coverage
                row["review_required"] = record.review_required
                row["source_urls"] = source_urls
                yield row
        else:
            row = _base_row(person, metadata_map)
            for field in RESEARCH_FIELDS:
                row[field.value] = ""
                row[f"{field.value}_confidence"] = ""
                if provenance == "field":
                    row[f"{field.value}_source_urls"] = ""
            row["profile_confidence"] = ""
            row["coverage"] = ""
            row["legacy_coverage"] = ""
            row["review_required"] = ""
            row["source_urls"] = ""
            yield row


def _base_row(person, metadata_map, *, status=None) -> dict[str, Any]:
    row = {output: person.original_row.get(source, "") for source, output in metadata_map.items()}
    effective_status = status or person.status
    row["status"] = getattr(effective_status, "value", effective_status)
    row["error_code"] = person.error_code or ""
    return row


def _field_column_pairs() -> list[tuple[str, str]]:
    return [(field.value, f"{field.value}_confidence") for field in RESEARCH_FIELDS]


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
    return _compact_urls(
        (
            source.canonical_url or source.final_url or source.requested_url
            for source in result.sources
            if source_record_allowed(source)
        ),
        limit=10,
    )


def _compact_urls(values, *, limit: int | None = None) -> str:
    urls: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = str(value)
        if not url or url in seen:
            continue
        seen.add(url)
        if source_policy_allows(url):
            urls.append(url)
            if limit is not None and limit > 0 and len(urls) >= limit:
                break
    return "; ".join(urls if limit is None else urls[:limit])


def _safe_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _export_csv(rows: Iterable[dict[str, Any]], columns: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writerow({column: _safe_cell(column) for column in columns})
    for row in rows:
        writer.writerow({column: _safe_cell(row.get(column, "")) for column in columns})
    return output.getvalue().encode("utf-8-sig")


def _export_xlsx(
    rows: Iterable[dict[str, Any]],
    columns: list[str],
    *,
    field_columns: list[tuple[str, str]] = (),
    low_confidence_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> bytes:
    if not 0 <= low_confidence_threshold <= 100:
        raise ValueError("Low-confidence threshold must be between 0 and 100")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Results"
    header_fill = PatternFill(fill_type="solid", fgColor="D9EAF7")
    header_font = Font(bold=True)
    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=_xlsx_cell(column))
        cell.font = header_font
        cell.fill = header_fill
    pale_yellow = PatternFill(fill_type="solid", fgColor="FFF2CC")
    column_indexes = {column: index for index, column in enumerate(columns, start=1)}
    widths = [len(str(column)) for column in columns]
    for row_index, row in enumerate(rows, start=2):
        for column_index, column in enumerate(columns, start=1):
            value = row.get(column, "")
            sheet.cell(row=row_index, column=column_index, value=_xlsx_cell(value))
            widths[column_index - 1] = max(widths[column_index - 1], len(str(value)))
        for value_column, confidence_column in field_columns:
            value = row.get(value_column)
            confidence = row.get(confidence_column)
            if (
                value not in {None, ""}
                and isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and confidence < low_confidence_threshold
            ):
                sheet.cell(row=row_index, column=column_indexes[value_column]).fill = pale_yellow
                sheet.cell(row=row_index, column=column_indexes[confidence_column]).fill = pale_yellow
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, width in enumerate(widths, start=1):
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
