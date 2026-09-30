"""Build the one export representation shared by downloads and explicit saves."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.config import DEFAULT_REVIEW_THRESHOLD
from app.export.formats import ProvenanceMode, export_results
from app.export.naming import export_filename, source_list_name
from app.schemas import JobResults, JobView

ExportFormat = Literal["csv", "xlsx"]


def export_media_type(format: ExportFormat) -> str:
    return (
        "text/csv; charset=utf-8"
        if format == "csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@dataclass(frozen=True)
class ExportArtifact:
    filename: str
    format: ExportFormat
    research_started_at: datetime
    list_name: str | None
    content: bytes


def build_export_artifact(
    results: JobResults,
    columns: list[str],
    job: JobView,
    format: ExportFormat,
    *,
    provenance: ProvenanceMode = "none",
    low_confidence_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> ExportArtifact:
    return ExportArtifact(
        filename=export_filename(results, job, format),
        format=format,
        research_started_at=job.started_at or job.created_at,
        list_name=source_list_name(results),
        content=export_results(
            results,
            columns,
            format,
            provenance=provenance,
            low_confidence_threshold=low_confidence_threshold,
        ),
    )
