"""POC artifact storage in the existing SQL database (BYTEA on PostgreSQL)."""

from datetime import timezone

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import undefer

from app.config import Settings
from app.db.models import SavedResultFileRow
from app.export.artifacts import ExportArtifact, ExportFormat
from app.library import LibraryLimitError, SavedFileContent, SavedResultFile
from app.schemas import new_id, utcnow

# Serialize only library mutations across API replicas. This does not lock jobs,
# claims, or worker activity in PostgreSQL.
_LIBRARY_LOCK_KEY = 0x4B4E4F57


class DatabaseResultFileStorage:
    def __init__(self, session_factory, settings: Settings):
        self.session_factory = session_factory
        self.settings = settings

    @staticmethod
    def _lock_writes(session) -> None:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LIBRARY_LOCK_KEY})
        elif dialect == "sqlite":
            # Acquire the SQLite write reservation before reading quota usage.
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            raise RuntimeError("Library storage requires PostgreSQL or SQLite")

    def save(self, artifact: ExportArtifact) -> SavedResultFile:
        """Retain the legacy single-format storage operation for existing adapters."""
        return self._save(artifact)

    def save_pair(self, csv: ExportArtifact, xlsx: ExportArtifact) -> SavedResultFile:
        if (
            csv.format != "csv"
            or xlsx.format != "xlsx"
            or csv.filename.rsplit(".", 1)[0] != xlsx.filename.rsplit(".", 1)[0]
            or csv.research_started_at != xlsx.research_started_at
            or csv.list_name != xlsx.list_name
        ):
            raise ValueError("Saved formats must describe the same result")
        return self._save(xlsx, csv)

    def _save(self, artifact: ExportArtifact, csv: ExportArtifact | None = None) -> SavedResultFile:
        # The HTTP API accepts a completed job, never an arbitrary file upload.
        # Only build_export_artifact produces this server-side object.
        for item in (artifact, csv) if csv else (artifact,):
            if item.format not in {"csv", "xlsx"}:
                raise ValueError("Unsupported library file format")
            if not 0 < len(item.content) <= self.settings.LIBRARY_MAX_FILE_BYTES:
                raise LibraryLimitError("Export exceeds LIBRARY_MAX_FILE_BYTES")
            if (
                not item.filename.endswith(f".{item.format}")
                or len(item.filename) > 200
                or any(character in item.filename for character in '/\\\r\n\x00"')
            ):
                raise ValueError("Unsafe library filename")
        size = len(artifact.content) + (len(csv.content) if csv else 0)
        row = SavedResultFileRow(
            file_id=new_id(),
            filename=artifact.filename,
            format=artifact.format,
            saved_at=utcnow(),
            research_started_at=artifact.research_started_at,
            size_bytes=size,
            list_name=artifact.list_name,
            content=artifact.content,
            csv_content=csv.content if csv else None,
            csv_size_bytes=len(csv.content) if csv else None,
        )
        with self.session_factory.begin() as session:
            self._lock_writes(session)
            count, used = session.execute(
                select(func.count(), func.coalesce(func.sum(SavedResultFileRow.size_bytes), 0)).select_from(
                    SavedResultFileRow
                )
            ).one()
            if count >= self.settings.LIBRARY_MAX_FILES:
                raise LibraryLimitError("Shared library has reached LIBRARY_MAX_FILES")
            if used + size > self.settings.LIBRARY_MAX_TOTAL_BYTES:
                raise LibraryLimitError("Shared library would exceed LIBRARY_MAX_TOTAL_BYTES")
            session.add(row)
        return self._metadata(row)

    def list_files(self, *, limit: int = 50, offset: int = 0) -> list[SavedResultFile]:
        with self.session_factory() as session:
            # Binary content is deferred by the model and never read for listing.
            rows = session.scalars(
                select(SavedResultFileRow)
                .order_by(SavedResultFileRow.saved_at.desc(), SavedResultFileRow.file_id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [self._metadata(row) for row in rows]

    def get(self, file_id: str, format: ExportFormat | None = None) -> SavedFileContent | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(SavedResultFileRow)
                .options(
                    undefer(SavedResultFileRow.csv_content if format == "csv" else SavedResultFileRow.content)
                )
                .where(SavedResultFileRow.file_id == file_id)
            )
            if row is None:
                return None
            metadata = self._metadata(row)
            chosen = format or row.format
            if chosen not in metadata.available_formats:
                return None
            companion = chosen == "csv" and row.csv_size_bytes is not None
            content = row.csv_content if companion else row.content
            if format is None:
                return SavedFileContent(metadata, content)
            return SavedFileContent(
                metadata.model_copy(
                    update={
                        "filename": row.filename.rsplit(".", 1)[0] + "." + chosen,
                        "format": chosen,
                        "size_bytes": len(content),
                    }
                ),
                content,
            )

    def delete(self, file_id: str) -> bool:
        with self.session_factory.begin() as session:
            self._lock_writes(session)
            return bool(
                session.execute(
                    delete(SavedResultFileRow).where(SavedResultFileRow.file_id == file_id)
                ).rowcount
            )

    @staticmethod
    def _metadata(row: SavedResultFileRow) -> SavedResultFile:
        def aware(value):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        return SavedResultFile(
            file_id=row.file_id,
            filename=row.filename,
            format=row.format,
            saved_at=aware(row.saved_at),
            research_started_at=aware(row.research_started_at),
            size_bytes=row.size_bytes,
            list_name=row.list_name,
            available_formats=["csv", "xlsx"] if row.csv_size_bytes is not None else [row.format],
        )
