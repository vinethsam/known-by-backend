"""Separate the public download contract from retained record details."""

from app.schemas import ProfileField

RESEARCH_FIELDS = tuple(ProfileField)

# One mapping is used by CSV, XLSX and historical Library downloads.
EXPORT_COLUMNS = {
    "full_name": "Full Name",
    "organisation": "Organisation",
    "job_title": "Job Title",
    "university_name": "University Name",
    "degree_type": "Degree Type",
    "subject": "Subject",
    "profile_link": "Profile Link",
    "full_name_confidence": "Full Name Confidence",
    "organisation_confidence": "Organisation Confidence",
    "job_title_confidence": "Job Title Confidence",
    "university_name_confidence": "University Confidence",
    "degree_type_confidence": "Degree Confidence",
    "subject_confidence": "Subject Confidence",
    "source_urls": "Source URLs",
}


def standard_columns() -> tuple[str, ...]:
    return tuple(EXPORT_COLUMNS.values())


def record_columns() -> tuple[str, ...]:
    """Private Library storage keeps the existing detailed viewer functional."""
    return (
        *(field.value for field in RESEARCH_FIELDS),
        *(f"{field.value}_confidence" for field in RESEARCH_FIELDS),
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
