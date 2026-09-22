"""Bounded chunk extraction, consumed in source order for stable evidence decisions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.prompts.extraction import EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT
from app.research.concurrency import ordered_window
from app.research.telemetry import count, stage
from app.schemas import ExtractionResponse, PersonSeed


async def extract_chunks(
    client,
    seed: PersonSeed,
    chunks: list[str],
    source_url: str,
    context: dict,
    semaphore: asyncio.Semaphore,
    concurrency: int,
) -> AsyncIterator[tuple[str, ExtractionResponse]]:
    payloads = [
        {
            "person": seed.model_dump(mode="json"),
            "source_url": source_url,
            "chunk_index": index,
            "content": chunk,
        }
        for index, chunk in enumerate(chunks)
    ]
    # Near a cap, retain the serial budget/claim order. Parallelize only when
    # every remaining chunk plus all configured retries fits the reservation.
    width = (
        concurrency
        if client.can_parallel_complete(ExtractionResponse, EXTRACTION_SYSTEM_PROMPT, payloads, context)
        else 1
    )

    async def extract(payload: dict) -> ExtractionResponse:
        async with semaphore:
            count("extraction_chunks")
            with stage("extraction_ms"):
                response, _ = await client.complete(
                    ExtractionResponse,
                    "extraction",
                    EXTRACTION_SYSTEM_PROMPT,
                    payload,
                    context,
                    EXTRACTION_PROMPT_VERSION,
                )
                return response

    async with ordered_window(payloads, extract, width) as results:
        async for payload, outcome in results:
            yield payload["content"], outcome.unwrap()
