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
    "review_reasons",
    "status",
    "record_status",
    "error_code",
    "source_urls",
)


def standard_columns() -> tuple[str, ...]:
    """The complete export schema, independent of input and provenance options."""

    return (
        *(field.value for field in RESEARCH_FIELDS),
        *(f"{field.value}_confidence" for field in RESEARCH_FIELDS),
        *provenance_columns(),
        *SUMMARY_COLUMNS,
    )


def provenance_columns() -> tuple[str, ...]:
    """Standard provenance distinguishes supplied certainty from research confidence."""

    return (
        *(
            f"{field.value}_{detail}"
            for field in RESEARCH_FIELDS
            for detail in (
                "input_confidence",
                "provenance_status",
                "supplied_values",
                "source_urls",
                "review_required",
                "review_reasons",
            )
        ),
    )
