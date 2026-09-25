from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from xml.etree.ElementTree import ParseError, XMLParser, iterparse

from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import ValidationError

from app.config import Settings
from app.input_validation import (
    MAX_INPUT_CELL_CHARS,
    MAX_INPUT_FILENAME_CHARS,
    MAX_INPUT_HEADER_CHARS,
    validate_input_text,
)
from app.schemas import PersonSeed


@dataclass(frozen=True)
class _TokenPattern:
    required: frozenset[str]
    any_of: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()


_PERSON_QUALIFIERS = frozenset(
    {
        "alumni",
        "candidate",
        "delegate",
        "employee",
        "individual",
        "member",
        "official",
        "participant",
        "person",
        "speaker",
        "staff",
    }
)
_NON_PERSON_NAME_TOKENS = frozenset(
    {
        "affiliation",
        "agency",
        "city",
        "code",
        "college",
        "company",
        "country",
        "department",
        "designation",
        "education",
        "email",
        "employer",
        "government",
        "id",
        "institution",
        "job",
        "location",
        "ministry",
        "number",
        "occupation",
        "office",
        "org",
        "organisation",
        "organization",
        "phone",
        "position",
        "region",
        "role",
        "school",
        "title",
        "university",
        "url",
        "website",
    }
)

# Central, deliberately small schema vocabulary. Strong aliases are preferred over
# constrained token patterns, which are preferred over generic one-word aliases.
COLUMN_ALIASES: dict[str, dict[str, frozenset[str]]] = {
    "full_name": {
        "strong": frozenset(
            {
                "alumni full name",
                "alumni name",
                "candidate name",
                "delegate name",
                "employee name",
                "full name",
                "individual name",
                "member name",
                "official name",
                "participant name",
                "person name",
                "speaker name",
                "staff name",
            }
        ),
        "generic": frozenset({"candidate", "name", "person"}),
    },
    "organisation": {
        "strong": frozenset(
            {
                "company name",
                "current employer",
                "employer name",
                "government body",
                "government office",
                "institution name",
                "organisation name",
                "organization name",
            }
        ),
        "generic": frozenset(
            {
                "affiliation",
                "agency",
                "company",
                "department",
                "employer",
                "institution",
                "ministry",
                "office",
                "org",
                "organisation",
                "organization",
            }
        ),
    },
    "job_title": {
        "strong": frozenset({"current job title", "current role", "job title"}),
        "generic": frozenset({"designation", "occupation", "position", "role", "title"}),
    },
    "country": {
        "strong": frozenset({"country name", "country of origin", "home country", "nationality"}),
        "generic": frozenset({"country"}),
    },
    "location": {
        "strong": frozenset({"city name", "current location", "home location", "region name"}),
        "generic": frozenset({"city", "location", "region"}),
    },
    "university_name": {
        "strong": frozenset(
            {
                "alma mater",
                "college name",
                "education institution",
                "educational institution",
                "institution attended",
                "school name",
                "university name",
            }
        ),
        "generic": frozenset({"college", "school", "university"}),
    },
    "subject": {
        "strong": frozenset({"academic subject", "course of study", "degree subject", "field of study"}),
        "generic": frozenset({"major", "subject"}),
    },
    "program_year": {
        "strong": frozenset(
            {
                "class year",
                "cohort year",
                "graduation year",
                "program year",
                "programme year",
                "year of graduation",
            }
        ),
        "generic": frozenset(),
    },
}

_COLLAPSED_ALIASES = {
    alias.replace(" ", ""): alias
    for groups in COLUMN_ALIASES.values()
    for alias in groups["strong"]
    if " " in alias
}

COLUMN_PATTERNS: dict[str, tuple[_TokenPattern, ...]] = {
    "full_name": (
        _TokenPattern(required=frozenset({"full", "name"}), excluded=_NON_PERSON_NAME_TOKENS),
        _TokenPattern(
            required=frozenset({"name"}),
            any_of=_PERSON_QUALIFIERS,
            excluded=_NON_PERSON_NAME_TOKENS,
        ),
    ),
    "organisation": (
        _TokenPattern(
            required=frozenset({"name"}),
            any_of=frozenset(
                {
                    "agency",
                    "company",
                    "department",
                    "employer",
                    "institution",
                    "ministry",
                    "office",
                    "org",
                    "organisation",
                    "organization",
                }
            ),
            excluded=frozenset({"college", "education", "school", "university"}),
        ),
        _TokenPattern(
            required=frozenset({"current"}),
            any_of=frozenset({"company", "employer", "institution", "organisation", "organization"}),
        ),
    ),
    "job_title": (
        _TokenPattern(required=frozenset({"job", "title"})),
        _TokenPattern(required=frozenset({"current"}), any_of=frozenset({"position", "role", "title"})),
    ),
    "country": (
        _TokenPattern(
            required=frozenset({"country"}),
            excluded=frozenset({"code", "id", "number"}),
        ),
    ),
    "location": (
        _TokenPattern(
            required=frozenset({"location"}),
            excluded=frozenset({"code", "id", "number"}),
        ),
    ),
    "university_name": (
        _TokenPattern(
            required=frozenset({"name"}),
            any_of=frozenset({"college", "school", "university"}),
        ),
        _TokenPattern(required=frozenset({"education", "institution"})),
        _TokenPattern(required=frozenset({"institution", "attended"})),
    ),
    "subject": (
        _TokenPattern(required=frozenset({"field", "study"})),
        _TokenPattern(required=frozenset({"degree", "subject"})),
    ),
    "program_year": (
        _TokenPattern(
            required=frozenset({"year"}),
            any_of=frozenset({"class", "cohort", "graduation", "program", "programme"}),
            excluded=frozenset({"birth"}),
        ),
    ),
}

_STRONG_ALIAS_SCORE = 300
_TOKEN_PATTERN_SCORE = 200
_GENERIC_ALIAS_SCORE = 100
_OBVIOUS_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_header(value: str) -> str:
    """Return the Unicode-safe comparison form used only for column headers."""

    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", normalized)
    normalized = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", normalized)
    separated = "".join(character if character.isalnum() else " " for character in normalized)
    comparison = " ".join(separated.casefold().split())
    return _COLLAPSED_ALIASES.get(comparison, comparison)


@dataclass(frozen=True)
class _HeaderMatch:
    column: str
    field: str
    score: int


@dataclass(frozen=True)
class _ColumnInference:
    name_column: str
    seed_columns: dict[str, str]


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
    _validate_text(filename, MAX_INPUT_FILENAME_CHARS)
    if name_column is not None:
        _validate_text(name_column, MAX_INPUT_HEADER_CHARS)
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
    _validate_xlsx_archive(content, settings)
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
    except (InvalidFileException, OSError, KeyError, ValueError, ParseError, zipfile.BadZipFile) as exc:
        raise BatchParseError("XLSX file is corrupted") from exc
    try:
        sheet = workbook.active
        if sheet is None:
            raise BatchParseError("XLSX file has no active worksheet")
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
    except (ParseError, KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile) as exc:
        if isinstance(exc, BatchParseError):
            raise
        raise BatchParseError("XLSX file is corrupted") from exc
    finally:
        workbook.close()


def _validate_xlsx_archive(content: bytes, settings: Settings) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 500:
                raise BatchParseError("XLSX file contains too many internal parts")
            if len({info.filename for info in entries}) != len(entries):
                raise BatchParseError("XLSX file contains duplicate internal parts")
            total = 0
            for info in entries:
                if info.flag_bits & 1:
                    raise BatchParseError("Encrypted XLSX files are not supported")
                total += info.file_size
                if total > settings.MAX_XLSX_UNCOMPRESSED_BYTES:
                    raise BatchParseError("XLSX file expands beyond the maximum allowed size")
                if info.compress_size and info.file_size / info.compress_size > 1000:
                    raise BatchParseError("XLSX compression ratio is too large")
            # Parse without building trees before openpyxl can expand entities or
            # allocate shared strings. The parser recognizes DTDs in UTF-16 too.
            for info in entries:
                if info.filename.lower().endswith((".xml", ".rels")):
                    parser = XMLParser(target=_BoundedXMLTarget())
                    with archive.open(info) as part:
                        while chunk := part.read(65536):
                            parser.feed(chunk)
                    parser.close()
    except (zipfile.BadZipFile, OSError, ValueError, ParseError, RuntimeError, NotImplementedError) as exc:
        if isinstance(exc, BatchParseError):
            raise
        raise BatchParseError("XLSX file is corrupted") from exc


class _BoundedXMLTarget:
    def start(self, tag, attributes):
        pass

    def end(self, tag):
        pass

    def data(self, value):
        pass

    def close(self):
        pass

    def doctype(self, name, public_id, system_id):
        raise BatchParseError("XLSX XML document types and entities are not supported")


def _rows_to_batch(rows: list[list[str]], name_column: str | None, settings: Settings) -> BatchInput:
    if not rows:
        raise BatchParseError("Batch file is empty")
    header = []
    for value in rows[0]:
        _validate_text(value, MAX_INPUT_HEADER_CHARS)
        header.append(value.strip())
    _validate_headers(header, settings)

    prepared_rows: list[tuple[int, list[str]]] = []
    for zero_based, raw_row in enumerate(rows[1:], start=2):
        if not raw_row:
            raise BatchParseError(f"Blank row at {zero_based}")
        if len(raw_row) > len(header):
            raise BatchParseError(f"Row {zero_based} contains data beyond the header")
        values = list(raw_row)
        if len(values) < len(header):
            values.extend([""] * (len(header) - len(values)))
        if not any(value.strip() for value in values):
            raise BatchParseError(f"Missing name in row {zero_based}")
        if len(prepared_rows) >= settings.MAX_BATCH_ROWS:
            raise BatchParseError("Batch file contains too many rows")
        prepared_rows.append((zero_based, values))

    inference = _infer_columns(header, [values for _, values in prepared_rows], name_column)
    output_rows: list[dict[str, Any]] = []
    seeds: list[PersonSeed] = []
    for row_number, values in prepared_rows:
        row = {column: values[index] for index, column in enumerate(header)}
        name = row[inference.name_column].strip()
        if not name:
            raise BatchParseError(f"Missing name in row {row_number}")
        if _obviously_not_person_name(name):
            raise BatchParseError(f"Invalid person name in row {row_number}")
        seed = _seed_from_row(row, name, inference.seed_columns)
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
    normalized = [unicodedata.normalize("NFC", column).casefold() for column in header]
    if len(set(normalized)) != len(normalized):
        raise BatchParseError("Batch file contains duplicate headers")


def _classify_header(column: str) -> _HeaderMatch | None:
    normalized = normalize_header(column)
    tokens = frozenset(normalized.split())
    scores: list[tuple[int, str]] = []
    for field_name, aliases in COLUMN_ALIASES.items():
        score = 0
        if normalized in aliases["strong"]:
            score = _STRONG_ALIAS_SCORE + len(tokens)
        elif normalized in aliases["generic"]:
            score = _GENERIC_ALIAS_SCORE
        for pattern in COLUMN_PATTERNS.get(field_name, ()):
            if (
                pattern.required <= tokens
                and (not pattern.any_of or bool(pattern.any_of & tokens))
                and not pattern.excluded.intersection(tokens)
            ):
                score = max(
                    score,
                    _TOKEN_PATTERN_SCORE + len(pattern.required) + bool(pattern.any_of),
                )
        if score:
            scores.append((score, field_name))
    if not scores:
        return None
    best_score = max(score for score, _ in scores)
    winners = [field_name for score, field_name in scores if score == best_score]
    if len(winners) != 1:
        return None
    return _HeaderMatch(column=column, field=winners[0], score=best_score)


def _infer_columns(
    header: list[str], data_rows: list[list[str]], name_column: str | None
) -> _ColumnInference:
    column_indexes = {column: index for index, column in enumerate(header)}
    matches = [match for column in header if (match := _classify_header(column)) is not None]
    if name_column:
        explicit = [
            (index, column)
            for index, column in enumerate(header)
            if unicodedata.normalize("NFC", column) == unicodedata.normalize("NFC", name_column)
        ]
        if not explicit:
            raise BatchParseError("Selected name column is not present")
        name_index, selected_name = explicit[0]
        if not _usable_name_column(name_index, data_rows):
            raise BatchParseError("Selected name column does not contain usable person names")
    else:
        name_candidates = [
            match
            for match in matches
            if match.field == "full_name" and _usable_name_column(column_indexes[match.column], data_rows)
        ]
        if not name_candidates:
            observed = ", ".join(repr(column) for column in header)
            raise BatchParseError(
                "Batch file is missing a name column; no usable person-name candidate "
                f"was inferred. Observed headers: {observed}"
            )
        best_score = max(match.score for match in name_candidates)
        strongest = [match for match in name_candidates if match.score == best_score]
        if len(strongest) > 1:
            candidates = ", ".join(repr(match.column) for match in strongest)
            raise BatchParseError(f"Batch file has ambiguous name columns: {candidates}")
        selected_name = strongest[0].column

    seed_columns: dict[str, str] = {}
    for field_name in COLUMN_ALIASES:
        if field_name == "full_name":
            continue
        candidates = [
            match for match in matches if match.field == field_name and match.column != selected_name
        ]
        if not candidates:
            continue
        best_score = max(match.score for match in candidates)
        strongest = [match for match in candidates if match.score == best_score]
        # Equally strong optional columns are retained in the original row but are
        # not guessed into a single-valued PersonSeed field.
        if len(strongest) == 1:
            seed_columns[field_name] = strongest[0].column
    return _ColumnInference(name_column=selected_name, seed_columns=seed_columns)


def _usable_name_column(index: int, data_rows: list[list[str]]) -> bool:
    if not data_rows:
        return True
    values = [row[index].strip() for row in data_rows if index < len(row) and row[index].strip()]
    return bool(values) and any(not _obviously_not_person_name(value) for value in values)


def _obviously_not_person_name(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"-", "n/a", "na", "none", "null", "unknown"}:
        return True
    if normalized.startswith(("http://", "https://", "www.")) or "://" in normalized:
        return True
    if _OBVIOUS_EMAIL.fullmatch(normalized):
        return True
    compact = "".join(character for character in normalized if character.isalnum())
    return not compact or compact.isdecimal() or not any(character.isalpha() for character in compact)


def _seed_from_row(row: dict[str, str], full_name: str, seed_columns: dict[str, str]) -> PersonSeed:
    payload: dict[str, Any] = {"full_name": full_name}
    for field_name, column in seed_columns.items():
        payload[field_name] = row[column].strip() or None
    try:
        return PersonSeed.model_validate(payload)
    except ValidationError as exc:
        raise BatchParseError(f"Invalid person seed: {exc.errors()[0]['msg']}") from exc


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    value = str(value)
    _validate_text(value, MAX_INPUT_CELL_CHARS, multiline=True)
    return value


def _validate_text(value: str, max_length: int, *, multiline: bool = False) -> None:
    try:
        validate_input_text(value, max_length=max_length, multiline=multiline)
    except ValueError as exc:
        raise BatchParseError(str(exc)) from exc


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
