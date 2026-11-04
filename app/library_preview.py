"""Bounded, read-only views of retained exports; never rerun research."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections.abc import Iterator
from typing import Any
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook

from app.config import DEFAULT_REVIEW_THRESHOLD
from app.export.formats import (
    COMBINED_EDUCATION_REASON,
    MAX_EXPORT_ROWS,
    ExportLimitError,
    _combine_education_rows,
    _compact_urls,
    _export_csv,
    _export_xlsx,
    _field_column_pairs,
    _validated_rows,
    standard_rows,
)
from app.export.schema import EXPORT_COLUMNS, RESEARCH_FIELDS, record_columns, standard_columns
from app.library import SavedFileContent, SavedResultFile
from app.research.normalisation import normalise_display
from app.schemas import Contract

MAX_PREVIEW_COLUMNS = 512
MAX_PREVIEW_ROWS = 100_000
MAX_CELL_CHARS = 32_767


class LibraryPreviewError(ValueError):
    """A retained artifact cannot be projected safely."""


class SavedRecordsPage(Contract):
    file: SavedResultFile
    records: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    has_more: bool


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _research_columns(keys: list[str]) -> list[tuple[int, str]]:
    """Recognize the old export prefix before its appended input metadata.

    Older exports can contain both research ``status`` and administrative
    ``STATUS``. Only the known generated prefix (and optional provenance block)
    belongs to that format's research result; later input columns are not evidence.
    """
    fields = [field.value for field in RESEARCH_FIELDS]
    if keys[:2] == ["full_name", "country_of_origin"]:
        fields.insert(1, "country_of_origin")
    legacy_prefix = [
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
    legacy_provenance = [
        *(f"{field}_source_urls" for field in fields),
        *(
            f"{field}_{detail}"
            for field in fields
            for detail in ("supplied_values", "input_confidence", "research_confidence", "provenance_status")
        ),
        "seed_input_metadata",
    ]
    legacy = keys[: len(legacy_prefix)] == legacy_prefix
    if legacy:
        stop = len(legacy_prefix)
        if keys[stop : stop + len(legacy_provenance)] == legacy_provenance:
            stop += len(legacy_provenance)
        keys = keys[:stop]
    known = set(record_columns())
    selected = [(index, key) for index, key in enumerate(keys) if key in known]
    if len({key for _, key in selected}) != len(selected):
        raise LibraryPreviewError("Saved file has duplicate research columns")
    if legacy:
        # The old confidence column sometimes represented input certainty. Use
        # the explicit research value when the artifact actually retained it.
        for field in fields:
            research_key = f"{field}_research_confidence"
            if research_key in keys:
                target = f"{field}_confidence"
                selected = [(index, key) for index, key in selected if key != target]
                selected.append((keys.index(research_key), target))
    return selected


def _cell(value: Any) -> str | int | float | bool:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            raise LibraryPreviewError("A saved cell is too large to preview")
        return value
    return str(value)


def _csv_rows(content: bytes) -> Iterator[list[Any]]:
    # Each row is consumed independently; no dataframe or full-file row list.
    with io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", newline="") as stream:
        yield from csv.reader(stream, strict=True)


def _xlsx_rows(content: bytes, max_uncompressed_bytes: int) -> Iterator[list[Any]]:
    with ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or sum(item.file_size for item in entries) > max_uncompressed_bytes:
            raise LibraryPreviewError("Saved workbook is too large to preview")
    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
    try:
        if not workbook.worksheets:
            raise LibraryPreviewError("Saved workbook has no records worksheet")
        sheet = workbook.worksheets[0]
        if sheet.max_column and sheet.max_column > MAX_PREVIEW_COLUMNS:
            raise LibraryPreviewError("Saved workbook has too many columns to preview")
        # Formula strings are returned as inert text. No formulas, macros or links
        # are evaluated, and the original bytes are never rewritten.
        for row in sheet.iter_rows(values_only=True):
            yield list(row)
    finally:
        workbook.close()


def _saved_records(
    saved: SavedFileContent,
    *,
    max_file_bytes: int = 10_000_000,
    max_uncompressed_bytes: int = 30_000_000,
) -> Iterator[dict[str, Any]]:
    if len(saved.content) > max_file_bytes:
        raise LibraryPreviewError("Saved file is too large to preview")
    rows = (
        _csv_rows(saved.content)
        if saved.metadata.format == "csv"
        else _xlsx_rows(saved.content, max_uncompressed_bytes)
    )
    try:
        header = next(rows, None)
        if not header or len(header) > MAX_PREVIEW_COLUMNS:
            raise LibraryPreviewError("Saved file has no usable research header")
        aliases = {_key(label): key for key, label in EXPORT_COLUMNS.items()}
        keys = [aliases.get(_key(value), _key(value)) for value in header]
        core = {field.value for field in RESEARCH_FIELDS}
        if not core.intersection(keys):
            raise LibraryPreviewError("Saved file has no recognized research columns")
        selected = _research_columns(keys)
        for index, cells in enumerate(rows):
            if index >= MAX_PREVIEW_ROWS:
                raise LibraryPreviewError("Saved file has too many rows to preview")
            if len(cells) > MAX_PREVIEW_COLUMNS:
                raise LibraryPreviewError("Saved file has too many columns to preview")
            if not any(value not in (None, "") for value in cells):
                continue
            record = {key: "" for key in record_columns()}
            for position, key in selected:
                record[key] = _cell(cells[position]) if position < len(cells) else ""
            for field in RESEARCH_FIELDS:
                value = record.get(field.value)
                if (
                    isinstance(value, str)
                    and value
                    and not value.startswith(("=", "+", "@"))
                    and not re.search(r"(?:^|\n)\d+\. ", value)
                    and COMBINED_EDUCATION_REASON not in str(record.get("review_reasons", ""))
                ):
                    record[field.value] = normalise_display(field, value)
            yield record
    except LibraryPreviewError:
        raise
    except (UnicodeError, csv.Error, BadZipFile, ValueError, KeyError, OSError, TypeError, ParseError) as exc:
        raise LibraryPreviewError("Saved file cannot be read; the original artifact remains stored") from exc
    finally:
        rows.close()


def preview_saved_file(
    saved: SavedFileContent,
    *,
    limit: int = 50,
    offset: int = 0,
    query: str = "",
    max_file_bytes: int = 10_000_000,
    max_uncompressed_bytes: int = 30_000_000,
) -> SavedRecordsPage:
    if not 1 <= limit <= 100 or offset < 0:
        raise LibraryPreviewError("Invalid record page")
    records = []
    matched = 0
    search = query.strip().casefold()
    for record in _saved_records(
        saved, max_file_bytes=max_file_bytes, max_uncompressed_bytes=max_uncompressed_bytes
    ):
        if search and not any(
            search in str(record.get(field.value, "")).casefold() for field in RESEARCH_FIELDS
        ):
            continue
        if offset <= matched < offset + limit:
            records.append(record)
        matched += 1
    return SavedRecordsPage(
        file=saved.metadata,
        records=records,
        total=matched,
        limit=limit,
        offset=offset,
        has_more=offset + len(records) < matched,
    )


def download_saved_file(
    saved: SavedFileContent,
    format: str,
    *,
    max_file_bytes=10_000_000,
    max_uncompressed_bytes=30_000_000,
    low_confidence_threshold=DEFAULT_REVIEW_THRESHOLD,
) -> bytes:
    """Project retained artifacts without updating their original bytes or metadata."""
    rows = list(
        _saved_records(saved, max_file_bytes=max_file_bytes, max_uncompressed_bytes=max_uncompressed_bytes)
    )
    for row in rows:
        row["_review_fields"] = set()
        for key in ("profile_confidence", "coverage", "legacy_coverage"):
            try:
                row[key] = float(row.get(key) or 0)
            except (ValueError, TypeError):
                row[key] = 0
        evidence = []
        for field in RESEARCH_FIELDS:
            key = field.value
            for suffix in ("confidence", "input_confidence"):
                try:
                    score = float(str(row.get(f"{key}_{suffix}", "")).removesuffix("%"))
                    row[f"{key}_{suffix}"] = min(100, max(0, score)) if math.isfinite(score) else 0
                except (ValueError, TypeError):
                    row[f"{key}_{suffix}"] = 0
            if row.get(f"{key}_provenance_status") == "supplied":
                row[f"{key}_confidence"] = 0
            if (
                key == "full_name"
                and row.get(f"{key}_input_confidence") == 100
                and row.get(f"{key}_provenance_status") != "corroborated"
            ):
                row[f"{key}_confidence"] = 0
            urls = row.get(f"{key}_source_urls", "")
            evidence.extend(re.split(r";\s*|\r?\n", str(urls)))
            if str(row.get(f"{key}_review_required", "")).lower() in {"true", "yes", "1"}:
                row["_review_fields"].add(key)
            if not row.get(f"{key}_supplied_values"):
                row[f"{key}_supplied_values"] = "[]"
        aggregate = row.get("source_urls", "")
        try:
            parsed = json.loads(aggregate)
        except (ValueError, TypeError):
            parsed = None
        evidence.extend(parsed if isinstance(parsed, list) else re.split(r";\s*|\r?\n", str(aggregate)))
        row["source_urls"] = _compact_urls(evidence)
    if len(rows) > MAX_EXPORT_ROWS:
        groups = {}
        for row in rows:
            identity = tuple(
                row.get(key, "") for key in ("full_name", "organisation", "job_title", "profile_link")
            )
            if not identity[0]:
                raise ExportLimitError("Saved export exceeds the 500-row limit")
            groups.setdefault(identity, []).append(row)
        if len(groups) > MAX_EXPORT_ROWS:
            raise ExportLimitError("Saved export exceeds the 500-person limit")
        rows = [_combine_education_rows(group) if len(group) > 1 else group[0] for group in groups.values()]
    columns = list(standard_columns())
    projected = _validated_rows(standard_rows(rows), columns)
    if format == "csv":
        return _export_csv(projected, columns)
    return _export_xlsx(
        projected,
        columns,
        field_columns=_field_column_pairs(),
        low_confidence_threshold=low_confidence_threshold,
    )
