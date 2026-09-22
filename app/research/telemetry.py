"""One compact performance event per person; no input text or new database rows."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

logger = logging.getLogger(__name__)
TIMINGS = (
    "queue_wait_ms",
    "discovery_ms",
    "source_validation_ms",
    "retrieval_ms",
    "processing_ms",
    "extraction_ms",
    "reconciliation_ms",
    "persistence_ms",
    "total_person_ms",
)
COUNTERS = (
    "search_queries",
    "search_tool_calls",
    "candidates_discovered",
    "candidates_selected",
    "sources_requested",
    "sources_retrieved",
    "retrieval_cache_hits",
    "retrieval_cache_misses",
    "static_fetches",
    "playwright_fallbacks",
    "extraction_calls",
    "extraction_chunks",
    "input_tokens",
    "output_tokens",
    "provider_cost",
)


@dataclass
class PersonPerformance:
    values: dict[str, float | int] = field(default_factory=lambda: dict.fromkeys((*TIMINGS, *COUNTERS), 0))


_current: ContextVar[PersonPerformance | None] = ContextVar("person_performance", default=None)


def count(name: str, amount: int | float = 1) -> None:
    current = _current.get()
    if current is not None:
        if name not in COUNTERS:
            raise ValueError("Unknown performance counter")
        current.values[name] += amount


@contextmanager
def stage(name: str) -> Iterator[None]:
    current = _current.get()
    if current is None:
        yield
        return
    if name not in TIMINGS:
        raise ValueError("Unknown performance stage")
    started = perf_counter()
    try:
        yield
    finally:
        current.values[name] += (perf_counter() - started) * 1000


@contextmanager
def person_performance(job_id: str, person_id: str, *, queue_wait_ms: float = 0) -> Iterator[None]:
    # The worker owns the outer lifetime including persistence. Direct research
    # callers still receive exactly one event through the nested orchestrator scope.
    if _current.get() is not None:
        yield
        return
    performance = PersonPerformance()
    performance.values["queue_wait_ms"] = max(0, queue_wait_ms)
    token = _current.set(performance)
    started = perf_counter()
    try:
        yield
    finally:
        performance.values["total_person_ms"] = (perf_counter() - started) * 1000
        _current.reset(token)
        logger.info(
            "person_performance",
            extra={
                "job_id": job_id,
                "person_id": person_id,
                "pipeline_stage": "person",
                "performance": {
                    key: round(value, 8 if key == "provider_cost" else 3)
                    if isinstance(value, float)
                    else value
                    for key, value in performance.values.items()
                },
            },
        )
