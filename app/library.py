"""Small artifact-storage boundary. There are no research entities in the library."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.export.artifacts import ExportArtifact, ExportFormat
from app.export.formats import ProvenanceMode
from app.schemas import Contract


class SaveResultRequest(Contract):
    format: ExportFormat = "xlsx"
    provenance: ProvenanceMode = "none"


class SavedResultFile(Contract):
    file_id: str
    filename: str
    format: ExportFormat
    saved_at: datetime
    research_started_at: datetime
    size_bytes: int
    list_name: str | None = None


@dataclass(frozen=True)
class SavedFileContent:
    metadata: SavedResultFile
    content: bytes


class LibraryLimitError(ValueError):
    """An explicit save would exceed an artifact or shared-library limit."""


class ResultFileStorage(Protocol):
    """A future object-storage adapter can implement this without changing the API."""

    def save(self, artifact: ExportArtifact) -> SavedResultFile: ...

    def list_files(self, *, limit: int, offset: int) -> list[SavedResultFile]: ...

    def get(self, file_id: str) -> SavedFileContent | None: ...

    def delete(self, file_id: str) -> bool: ...
