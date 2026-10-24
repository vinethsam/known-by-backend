"""Bounded semantic seed context; raw input never becomes provider context by default."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.input_validation import sanitize_input_text


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
_PASSIVE_SOURCE_METADATA_HEADERS = frozenset(
    {
        "cohort",
        "dataset name",
        "list name",
        "source cohort",
        "source dataset",
        "source list",
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
                "employer name",
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
                "office",
                "org",
                "organisation",
                "organization",
            }
        ),
    },
    "current_employer": {
        "strong": frozenset(
            {
                "current company",
                "current company name",
                "current employer",
                "current employer name",
                "current organisation",
                "current organisation name",
                "current organization",
                "current organization name",
            }
        ),
        "generic": frozenset(),
    },
    "alumni_organisation": {
        "strong": frozenset(
            {
                "alumni company",
                "alumni company name",
                "alumni employer",
                "alumni employer name",
                "alumni organisation",
                "alumni organisation name",
                "alumni organization",
                "alumni organization name",
            }
        ),
        "generic": frozenset(),
    },
    "historical_employer": {
        "strong": frozenset(
            {
                "former company",
                "former company name",
                "former employer",
                "former employer name",
                "former organisation",
                "former organisation name",
                "former organization",
                "former organization name",
                "historical employer",
                "historical organisation",
                "historical organisation name",
                "historical organization",
                "historical organization name",
                "past employer",
                "past organisation",
                "past organisation name",
                "past organization",
                "past organization name",
                "previous company",
                "previous company name",
                "previous employer",
                "previous employer name",
                "previous organisation",
                "previous organisation name",
                "previous organization",
                "previous organization name",
            }
        ),
        "generic": frozenset(),
    },
    "government_office": {
        "strong": frozenset(
            {
                "government agency",
                "government agency name",
                "government body",
                "government body name",
                "government department",
                "government department name",
                "government ministry",
                "government ministry name",
                "government office",
                "government office name",
                "ministry name",
            }
        ),
        "generic": frozenset({"ministry"}),
    },
    "job_title": {
        "strong": frozenset({"current job title", "current role", "job title"}),
        "generic": frozenset({"designation", "occupation", "position", "role", "title"}),
    },
    "country": {
        "strong": frozenset({"country name", "current country", "residence country", "nationality"}),
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

_ADDITIONAL_ALIASES = {
    "degree_type": (
        {
            "degree type",
            "degree name",
            "academic degree",
            "education degree",
            "qualification type",
            "qualification name",
        },
        {"degree", "qualification", "credential"},
    ),
    "country_of_origin": (
        {"country of origin", "origin country", "home country", "birth country", "country of birth"},
        set(),
    ),
    "industry": (
        {
            "professional field",
            "professional industry",
            "industry name",
            "industry sector",
            "business sector",
        },
        {"industry", "sector", "profession", "specialism", "specialization"},
    ),
    "historical_job_title": (
        {
            f"{prefix} {role}"
            for prefix in ("previous", "former", "past", "historical")
            for role in ("job title", "role", "position", "title")
        },
        set(),
    ),
    "profile_link": (
        {"profile link", "profile url", "professional profile", "personal website"},
        {"website"},
    ),
}
for _field, (_strong, _generic) in _ADDITIONAL_ALIASES.items():
    COLUMN_ALIASES[_field] = {"strong": frozenset(_strong), "generic": frozenset(_generic)}
COLUMN_ALIASES["university_name"]["strong"] |= frozenset(
    {
        "education school name",
        "education school",
        "educational institution name",
        "university attended",
        "graduate university",
        "undergraduate university",
        "university college",
        "school attended",
    }
)
COLUMN_ALIASES["subject"]["strong"] |= frozenset(
    {"education major", "academic major", "study field", "study subject"}
)
COLUMN_ALIASES["organisation"]["strong"] |= frozenset(
    {"job company name", "work organisation", "work organization"}
)

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
    ),
    "current_employer": (
        _TokenPattern(
            required=frozenset({"current"}),
            any_of=frozenset({"company", "employer", "institution", "organisation", "organization"}),
        ),
    ),
    "alumni_organisation": (
        _TokenPattern(
            required=frozenset({"alumni"}),
            any_of=frozenset({"company", "employer", "institution", "organisation", "organization"}),
        ),
    ),
    "historical_employer": (
        _TokenPattern(
            any_of=frozenset({"company", "employer", "institution", "organisation", "organization"}),
            required=frozenset({"former"}),
        ),
        _TokenPattern(
            any_of=frozenset({"company", "employer", "institution", "organisation", "organization"}),
            required=frozenset({"previous"}),
        ),
        _TokenPattern(
            any_of=frozenset({"company", "employer", "institution", "organisation", "organization"}),
            required=frozenset({"historical"}),
        ),
        _TokenPattern(
            any_of=frozenset({"company", "employer", "institution", "organisation", "organization"}),
            required=frozenset({"past"}),
        ),
    ),
    "government_office": (
        _TokenPattern(
            required=frozenset({"government"}),
            any_of=frozenset({"agency", "body", "department", "ministry", "office"}),
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

    normalized = unicodedata.normalize("NFKC", sanitize_input_text(value)).strip()
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


def _classify_header(column: str) -> _HeaderMatch | None:
    normalized = normalize_header(column)
    # These labels identify the originating dataset. They stay on original_row and
    # must never be inferred into PersonSeed, even if aliases expand in the future.
    if seed_attribute_category(column) in {"administrative", "sensitive"}:
        return None
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


_ADMIN_TOKENS = frozenset(
    {
        "id",
        "ids",
        "identifier",
        "identifiers",
        "uuid",
        "batch",
        "dataset",
        "list",
        "record",
        "row",
        "source",
        "import",
        "imported",
        "updated",
        "created",
        "confidence",
        "score",
        "status",
        "error",
    }
)
_SENSITIVE_TOKENS = frozenset(
    {
        "email",
        "emails",
        "phone",
        "phones",
        "telephone",
        "mobile",
        "address",
        "addresses",
        "street",
        "postcode",
        "postal",
        "zip",
        "birthdate",
        "birthday",
        "dob",
        "passport",
        "ssn",
        "salary",
        "religion",
        "ethnicity",
        "gender",
    }
)
_CONTEXT_FIELDS = (
    "organisation",
    "university_name",
    "job_title",
    "degree_type",
    "subject",
    "industry",
    "historical_employer",
    "historical_job_title",
    "country_of_origin",
    "country",
    "location",
    "program_year",
)
_SEMANTIC_LABELS = {
    "organisation affiliation": "organisation_affiliation",
    "government office": "government_office",
    "alumni organisation": "alumni_organisation",
}
_MEANING_FIELDS = {*COLUMN_ALIASES, "organisation_affiliation"}


def seed_attribute_category(label: str) -> str:
    """Classify private/passive labels before any semantic alias inference."""
    normalized = normalize_header(label.removeprefix("known:"))
    tokens = frozenset(normalized.split())
    if tokens & _SENSITIVE_TOKENS or {"date", "birth"} <= tokens:
        return "sensitive"
    if tokens & _ADMIN_TOKENS or normalized in _PASSIVE_SOURCE_METADATA_HEADERS:
        return "administrative"
    return "context"


def classify_seed_label(label: str) -> str | None:
    label = label.removeprefix("known:")
    if seed_attribute_category(label) != "context":
        return None
    normalized = normalize_header(label)
    if normalized in _SEMANTIC_LABELS:
        return _SEMANTIC_LABELS[normalized]
    match = _classify_header(label)
    return match.field if match else None


def is_research_attribute(label: str) -> bool:
    return classify_seed_label(label) in {
        *_CONTEXT_FIELDS,
        "current_employer",
        "alumni_organisation",
        "government_office",
        "organisation_affiliation",
    }


def safe_context_value(value: Any) -> str | None:
    """Public-search context is short prose, never contact details or long blobs."""
    if not isinstance(value, str):
        return None
    value = " ".join(sanitize_input_text(value).split())
    if not value or len(value) > 200 or value.casefold() in {"unknown", "none", "null", "n/a", "na", "-"}:
        return None
    if "@" in value or re.search(r"https?://|www\.|\b\d[\d ()+.-]{6,}\d\b", value, re.I):
        return None
    if re.search(r"\b\d+\s+.*\b(?:street|road|avenue|lane|drive|boulevard|apt|suite)\b", value, re.I):
        return None
    return value


def _seed_value(seed, key: str, default=None):
    return seed.get(key, default) if isinstance(seed, Mapping) else getattr(seed, key, default)


def seed_context_values(seed) -> dict[str, list[str]]:
    """Return approved semantic values within the existing prompt/search budgets.

    Explicit typed values lead; additional recognized aliases stay distinct so an
    ambiguous optional column is useful without being guessed into a core field.
    """
    context: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    total_chars = 0

    def add(field: str | None, raw: Any) -> None:
        nonlocal total_chars
        if isinstance(raw, (list, tuple)):
            for item in raw[:20]:
                add(field, item)
            return
        if field not in {
            *_CONTEXT_FIELDS,
            "current_employer",
            "alumni_organisation",
            "government_office",
            "organisation_affiliation",
        }:
            return
        value = safe_context_value(raw)
        if value is None or len(seen) >= 20 or total_chars + len(value) > 2200:
            return
        if field == "current_employer":
            field = "organisation"
        marker = (field, unicodedata.normalize("NFKC", value).casefold())
        if marker in seen:
            return
        seen.add(marker)
        total_chars += len(value)
        context.setdefault(field, []).append(value)

    for field in _CONTEXT_FIELDS:
        add(field, _seed_value(seed, field))
    for label, raw in (_seed_value(seed, "known_attributes", {}) or {}).items():
        add(classify_seed_label(label), raw)
    for label, raw in (_seed_value(seed, "input_metadata", {}) or {}).items():
        # Classify the original label again; a caller-controlled map cannot turn a
        # sensitive/administrative header into public research context.
        meaning = classify_seed_label(label)
        add(meaning, raw)
    return context


def seed_input_attributes(seed) -> list[dict[str, Any]]:
    """Audit every supplied value, including passive/private/unknown metadata."""
    attributes = []
    metadata = _seed_value(seed, "input_metadata", {}) or {}
    field_map = _seed_value(seed, "input_field_map", {}) or {}
    for label, raw in metadata.items():
        category = seed_attribute_category(label)
        field = classify_seed_label(label)
        if category == "context" and field is None and field_map.get(label) in _MEANING_FIELDS:
            field = field_map[label]
        if category == "context" and field is None:
            category = "unknown"
        attributes.append(
            {
                "column": label,
                "value": raw,
                "field": field,
                "category": category,
                "input_confidence": 100,
                "provenance_status": "supplied",
            }
        )
    # Older persisted seeds predate input_metadata. Keep their explicit context.
    if not metadata:
        for field in ("full_name", *_CONTEXT_FIELDS, "profile_link"):
            value = _seed_value(seed, field)
            if value is not None:
                attributes.append(
                    {
                        "column": field,
                        "value": value,
                        "field": field,
                        "category": "context",
                        "input_confidence": 100,
                        "provenance_status": "supplied",
                    }
                )
        for label, value in (_seed_value(seed, "known_attributes", {}) or {}).items():
            field = classify_seed_label(label)
            category = seed_attribute_category(label)
            attributes.append(
                {
                    "column": label,
                    "value": value,
                    "field": field,
                    "category": category if category != "context" or field else "unknown",
                    "input_confidence": 100,
                    "provenance_status": "supplied",
                }
            )
    return attributes


def seed_core_values(seed) -> dict[str, str]:
    """Core supplied values only; former employment is never current employment."""
    return {
        field: value
        for field in (
            "full_name",
            "country_of_origin",
            "organisation",
            "job_title",
            "university_name",
            "degree_type",
            "subject",
            "profile_link",
        )
        if (value := _seed_value(seed, field))
    }
