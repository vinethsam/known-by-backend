"""The stable research-export header contract shared by all file formats."""

from __future__ import annotations

from app.schemas import ProfileField

# Keep the researched value columns together. Changing their presentation order
# requires editing only this registry, not the CSV/XLSX or library code paths.
RESEARCH_FIELDS: tuple[ProfileField, ...] = (
    ProfileField.full_name,
    ProfileField.country_of_origin,
    ProfileField.organisation,
    ProfileField.job_title,
    ProfileField.university_name,
    ProfileField.degree_type,
    ProfileField.subject,
    ProfileField.profile_link,
)

SUMMARY_COLUMNS: tuple[str, ...] = (
    "profile_confidence",
    "coverage",
    "legacy_coverage",
    "review_required",
    "status",
    "error_code",
    "source_urls",
)


def standard_columns() -> tuple[str, ...]:
    """Return the fixed prefix, independent of input columns and provenance mode."""

    return (
        *(field.value for field in RESEARCH_FIELDS),
        *(f"{field.value}_confidence" for field in RESEARCH_FIELDS),
        *SUMMARY_COLUMNS,
    )


def provenance_columns() -> tuple[str, ...]:
    """Optional field URLs follow the stable prefix without moving its columns."""

    return (
        *(f"{field.value}_source_urls" for field in RESEARCH_FIELDS),
        *(
            f"{field.value}_{detail}"
            for field in RESEARCH_FIELDS
            for detail in ("supplied_values", "input_confidence", "research_confidence", "provenance_status")
        ),
        "seed_input_metadata",
    )
