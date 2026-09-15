from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from xml.etree.ElementTree import ParseError, iterparse

from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import ValidationError

from app.config import Settings
from app.schemas import PersonSeed

NAME_ALIASES = {"name", "full_name", "full name"}


@dataclass(frozen=True)
class BatchInput:
    seeds: list[PersonSeed]
    rows: list[dict[str, Any]]
    columns: list[str]


class BatchParseError(ValueError):
    pass


def parse_batch(
    content: bytes,
    filename: str,
    name_column: str | None,
    settings: Settings,
) -> BatchInput:
    if len(content) > settings.MAX_UPLOAD_BYTES:
        raise BatchParseError("Batch upload exceeds maximum size")
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".xlsx":
        rows = _read_xlsx(content, settings)
    elif suffix == ".tsv":
        rows = _read_delimited(content, "\t", settings)
    elif suffix == ".csv" or not suffix:
        rows = _read_delimited(content, ",", settings)
    else:
        raise BatchParseError("Unsupported batch file type")
    return _rows_to_batch(rows, name_column, settings)


def _read_delimited(content: bytes, delimiter: str, settings: Settings) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BatchParseError("Batch file must be UTF-8 text") from exc
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    rows: list[list[str]] = []
    try:
        for row_number, row in enumerate(reader, start=1):
            if row_number > settings.MAX_BATCH_ROWS + 1:
                raise BatchParseError("Batch file contains too many rows")
            if len(row) > settings.MAX_INPUT_COLUMNS:
                raise BatchParseError("Batch file contains too many columns")
            rows.append([_stringify_cell(value) for value in row])
    except csv.Error as exc:
        raise BatchParseError("CSV is malformed or contains an oversized cell") from exc
    return rows


def _read_xlsx(content: bytes, settings: Settings) -> list[list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 500:
                raise BatchParseError("XLSX file contains too many internal parts")
            total = 0
            for info in entries:
                total += info.file_size
                if total > settings.MAX_XLSX_UNCOMPRESSED_BYTES:
                    raise BatchParseError("XLSX file expands beyond the maximum allowed size")
                if info.compress_size and info.file_size / info.compress_size > 1000:
                    raise BatchParseError("XLSX compression ratio is too large")
    except zipfile.BadZipFile as exc:
        raise BatchParseError("XLSX file is corrupted") from exc

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except (InvalidFileException, OSError, KeyError, ValueError) as exc:
        raise BatchParseError("XLSX file is corrupted") from exc
    try:
        sheet = workbook.active
        _validate_xlsx_dimensions(sheet, settings)
        # Validate actual cell coordinates as well as the declared worksheet range.
        # A forged small dimension must not silently hide rows/columns from a batch.
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            with archive.open(sheet._worksheet_path) as xml:
                for _, element in iterparse(xml, events=("end",)):
                    if element.tag.endswith("}c") and element.get("r"):
                        _, _, column, row_index = range_boundaries(element.get("r"))
                        if column > settings.MAX_INPUT_COLUMNS or row_index > settings.MAX_BATCH_ROWS + 1:
                            raise BatchParseError("XLSX cells exceed configured rows or columns")
                    element.clear()
        sheet.reset_dimensions()
        rows: list[list[str]] = []
        for row_number, row in enumerate(
            sheet.iter_rows(),
            start=1,
        ):
            if row_number > settings.MAX_BATCH_ROWS + 1:
                raise BatchParseError("Batch file contains too many rows")
            values = [_stringify_cell(cell.value) for cell in row]
            if len(values) > settings.MAX_INPUT_COLUMNS:
                raise BatchParseError("Batch file contains too many columns")
            rows.append(values)
        return rows
    except (ParseError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, BatchParseError):
            raise
        raise BatchParseError("XLSX file is corrupted") from exc
    finally:
        workbook.close()


def _rows_to_batch(rows: list[list[str]], name_column: str | None, settings: Settings) -> BatchInput:
    if not rows:
        raise BatchParseError("Batch file is empty")
    header = [_stringify_cell(value).strip() for value in rows[0]]
    _validate_headers(header, settings)
    name_key = _select_name_column(header, name_column)

    output_rows: list[dict[str, Any]] = []
    seeds: list[PersonSeed] = []
    for zero_based, raw_row in enumerate(rows[1:], start=2):
        if not raw_row:
            raise BatchParseError(f"Blank row at {zero_based}")
        if len(raw_row) > len(header):
            raise BatchParseError(f"Row {zero_based} contains data beyond the header")
        values = [_stringify_cell(value) for value in raw_row]
        if len(values) < len(header):
            values.extend([""] * (len(header) - len(values)))
        if not any(value.strip() for value in values):
            raise BatchParseError(f"Missing name in row {zero_based}")
        if len(output_rows) >= settings.MAX_BATCH_ROWS:
            raise BatchParseError("Batch file contains too many rows")
        row = {column: values[index] for index, column in enumerate(header)}
        name = row[name_key].strip()
        if not name:
            raise BatchParseError(f"Missing name in row {zero_based}")
        seed = _seed_from_row(row, name)
        output_rows.append(row)
        seeds.append(seed)
    if not seeds:
        raise BatchParseError("Batch file contains no people")
    return BatchInput(seeds=seeds, rows=output_rows, columns=header)


def _validate_headers(header: list[str], settings: Settings) -> None:
    if not header:
        raise BatchParseError("Batch file is missing a header row")
    if len(header) > settings.MAX_INPUT_COLUMNS:
        raise BatchParseError("Batch file contains too many columns")
    if any(not column for column in header):
        raise BatchParseError("Batch file contains a blank header")
    normalized = [column.casefold() for column in header]
    if len(set(normalized)) != len(normalized):
        raise BatchParseError("Batch file contains duplicate headers")


def _select_name_column(header: list[str], name_column: str | None) -> str:
    if name_column:
        matches = [column for column in header if column == name_column]
        if not matches:
            raise BatchParseError("Selected name column is not present")
        return matches[0]
    matches = [column for column in header if column.strip().casefold() in NAME_ALIASES]
    if not matches:
        raise BatchParseError("Batch file is missing a name column")
    if len(matches) > 1:
        raise BatchParseError("Batch file has ambiguous name columns")
    return matches[0]


def _seed_from_row(row: dict[str, str], full_name: str) -> PersonSeed:
    seed_fields = {
        "organisation",
        "country",
        "location",
        "university_name",
        "job_title",
        "subject",
        "program_year",
    }
    payload: dict[str, Any] = {"full_name": full_name}
    for column, value in row.items():
        stripped = value.strip()
        key = column.strip().casefold().replace(" ", "_")
        if key in seed_fields:
            payload[key] = stripped or None
    try:
        return PersonSeed.model_validate(payload)
    except ValidationError as exc:
        raise BatchParseError(f"Invalid person seed: {exc.errors()[0]['msg']}") from exc


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _validate_xlsx_dimensions(sheet, settings: Settings) -> None:
    try:
        minimum_column, minimum_row, maximum_column, maximum_row = range_boundaries(
            sheet.calculate_dimension()
        )
    except ValueError:
        # Unsized sheets are checked through actual XML coordinates above.
        return
    if minimum_column < 1 or minimum_row < 1:
        raise BatchParseError("XLSX file has invalid dimensions")
    if maximum_column > settings.MAX_INPUT_COLUMNS:
        raise BatchParseError("Batch file contains too many columns")
    if maximum_row > settings.MAX_BATCH_ROWS + 1:
        raise BatchParseError("Batch file contains too many rows")
