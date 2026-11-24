"""Explicit in-memory evidence capture for a single research person."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from app.processing import PreparedContent
from app.retrieval.contracts import RetrievedPage
from app.schemas import PersonSeed, SourceRecord


@dataclass(frozen=True)
class PreparedShadowSource:
    source: SourceRecord
    page: RetrievedPage
    content: PreparedContent
    chunks: tuple[str, ...]


class ShadowSnapshot(NamedTuple):
    person_id: str
    seed: PersonSeed
    sources: tuple[PreparedShadowSource, ...]
    omitted_source_ids: tuple[str, ...]


class ExtractionShadow:
    """Copy bounded admitted evidence only when explicitly passed to research().

    No extractor, client, log, or persistence is used by this capture object.
    The caller owns the lifetime of potentially sensitive captured evidence.
    """

    def __init__(self, *, max_sources: int = 6, max_bytes: int = 16_000_000):
        if max_sources <= 0 or max_bytes <= 0:
            raise ValueError("Shadow capture limits must be positive")
        self._max_sources = max_sources
        self._max_bytes = max_bytes
        self._person_id: str | None = None
        self._seed: PersonSeed | None = None
        self._captured: dict[str, tuple[int, PreparedShadowSource]] = {}
        self._omitted: set[str] = set()

    def observe(
        self,
        seed: PersonSeed,
        source: SourceRecord,
        page: RetrievedPage,
        content: PreparedContent,
        chunks: Sequence[str],
    ) -> None:
        if self._seed is None:
            self._person_id = source.person_id
            self._seed = seed.model_copy(deep=True)
        if self._person_id != source.person_id or self._seed != seed:
            raise ValueError("Use a separate shadow capture for each research person")
        # Count serialized evidence, including the raw page used by the link extractor.
        # Stored source text is already represented by the frozen page and chunks.
        size = len(page.model_dump_json().encode()) + len(content.text.encode())
        size += sum(
            len(block.encode()) + len(response.model_dump_json().encode())
            for block, response in content.structured
        )
        size += sum(len(chunk.encode()) for chunk in chunks)
        self._captured.pop(source.source_id, None)
        if len(self._captured) >= self._max_sources or (
            sum(captured_size for captured_size, _ in self._captured.values()) + size > self._max_bytes
        ):
            self.note_omission(source.source_id)
            return
        self._omitted.discard(source.source_id)
        self._captured[source.source_id] = (
            size,
            PreparedShadowSource(
                source.model_copy(update={"raw_content": None, "compacted_text": None}, deep=True),
                page.model_copy(deep=True),
                PreparedContent(
                    content.text,
                    [(block, response.model_copy(deep=True)) for block, response in content.structured],
                ),
                tuple(chunks),
            ),
        )

    def note_omission(self, source_id: str) -> None:
        """Mark a failed or capped observation so comparison stays incomplete."""
        self._captured.pop(source_id, None)
        self._omitted.add(source_id)

    def snapshot(self) -> ShadowSnapshot:
        if self._person_id is None or self._seed is None:
            raise ValueError("No admitted sources have reached the shadow capture")
        return ShadowSnapshot(
            self._person_id,
            self._seed.model_copy(deep=True),
            tuple(source for _, source in self._captured.values()),
            tuple(sorted(self._omitted)),
        )
