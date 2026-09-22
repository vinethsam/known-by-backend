"""Bounded speculative I/O with deterministic consumption and owned cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class Outcome(Generic[R]):
    value: R | None = None
    error: Exception | None = None

    def unwrap(self) -> R:
        if self.error is not None:
            raise self.error
        return self.value  # type: ignore[return-value]


@asynccontextmanager
async def ordered_window(
    items: Sequence[T], operation: Callable[[T], Awaitable[R]], limit: int | Callable[[], int]
) -> AsyncIterator[AsyncIterator[tuple[T, Outcome[R]]]]:
    """At most limit tasks exist; errors are raised by consumers in input order."""
    pending: dict[int, asyncio.Task[Outcome[R]]] = {}

    async def run(item: T) -> Outcome[R]:
        try:
            return Outcome(value=await operation(item))
        except Exception as exc:
            return Outcome(error=exc)

    async def results() -> AsyncIterator[tuple[T, Outcome[R]]]:
        next_index = 0
        for index, item in enumerate(items):
            width = limit() if callable(limit) else max(1, limit)
            if width <= 0:
                return
            while next_index < min(len(items), index + width):
                pending[next_index] = asyncio.create_task(run(items[next_index]))
                next_index += 1
            outcome = await pending[index]
            del pending[index]
            yield item, outcome

    try:
        yield results()
    finally:
        tasks = list(pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
