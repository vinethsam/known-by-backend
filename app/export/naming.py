"""Stable, human-readable names shared by downloads and retained exports."""

from __future__ import annotations

import unicodedata
from datetime import timezone
from typing import Literal
from urllib.parse import quote

from app.export.batch import normalize_header
from app.schemas import JobResults, JobView

ExportFormat = Literal["csv", "xlsx"]
MAX_LIST_NAME_CHARS = 100
MAX_LIST_NAME_BYTES = 180
_SOURCE_LIST_HEADERS = frozenset(
    {"list name", "source list", "source dataset", "dataset name", "cohort", "source cohort"}
)


def source_list_name(results: JobResults) -> str | None:
    """Read the first nonempty list value without changing any original input row."""

    for person in sorted(results.people, key=lambda person: person.row_index):
        for header, value in person.original_row.items():
            if normalize_header(header) not in _SOURCE_LIST_HEADERS or value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return None


def safe_list_name(value: str | None) -> str:
    """Keep readable Unicode within conservative filesystem name limits."""

    characters: list[str] = []
    for character in unicodedata.normalize("NFC", value or ""):
        category = unicodedata.category(character)
        if character.isspace():
            characters.append("-")
        elif category.startswith("C"):
            continue
        elif category[0] in {"L", "N", "M"}:
            characters.append(character)
        else:
            characters.append("-")
    slug = "-".join(part for part in "".join(characters).split("-") if part)
    slug = slug[:MAX_LIST_NAME_CHARS]
    # Some Unicode filenames exceed filesystem byte limits before the character
    # limit is reached. Never cut inside a UTF-8 code point.
    slug = slug.encode("utf-8")[:MAX_LIST_NAME_BYTES].decode("utf-8", errors="ignore").rstrip("-")
    return slug or "KnownBy"


def export_filename(results: JobResults, job: JobView, format: ExportFormat) -> str:
    """Use the job start in UTC; unstarted jobs fall back to their creation time."""

    if format not in {"csv", "xlsx"}:
        raise ValueError("Unsupported export format")
    timestamp = job.started_at or job.created_at
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc)
    return f"{safe_list_name(source_list_name(results))}_{timestamp:%Y-%m-%d_%H%M}.{format}"


def content_disposition(filename: str) -> str:
    """Supply an ASCII fallback plus RFC 5987 encoding for Unicode downloads."""

    if not filename or any(
        character in '/\\"' or unicodedata.category(character).startswith("C") for character in filename
    ):
        raise ValueError("Unsafe export filename")
    transliterated = unicodedata.normalize("NFKD", filename).encode("ascii", errors="ignore").decode("ascii")
    fallback = "".join(
        character if character.isalnum() or character in "-_." else "-" for character in transliterated
    )
    if fallback.startswith(("_", ".", "-")):
        fallback = "KnownBy" + fallback
    fallback = fallback or "KnownBy"
    header = f'attachment; filename="{fallback}"'
    if filename != fallback:
        header += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return header
