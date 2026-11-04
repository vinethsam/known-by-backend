"""Bounded chunk extraction, consumed in source order for stable evidence decisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator

from app.prompts.extraction import EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT
from app.research.concurrency import ordered_window
from app.research.identity import research_seed_payload
from app.research.telemetry import count, stage
from app.retrieval.urls import canonicalise_url, source_policy_allows
from app.schemas import ExtractionResponse, PersonSeed


def extraction_chunk_key(
    source_id: str,
    chunk: str,
    *,
    person_id: str = "",
    source_url: str = "",
    model: str = "",
) -> str:
    """Bind applied evidence to its person, source, exact content and extraction rules."""
    envelope = [
        person_id,
        source_id,
        canonicalise_url(source_url) if source_url else "",
        chunk,
        model,
        EXTRACTION_PROMPT_VERSION,
        EXTRACTION_SYSTEM_PROMPT,
    ]
    return hashlib.sha256(json.dumps(envelope, ensure_ascii=True).encode("utf-8")).hexdigest()


async def extract_chunks(
    client,
    seed: PersonSeed,
    chunks: list[str],
    source_url: str,
    context: dict,
    semaphore: asyncio.Semaphore,
    concurrency: int,
    *,
    applied_chunks: set[str] | None = None,
) -> AsyncIterator[tuple[str, ExtractionResponse]]:
    if not source_policy_allows(source_url):
        return
    payloads = []
    model = getattr(getattr(client, "settings", None), "OPENROUTER_EXTRACTION_MODEL", "") or ""
    for index, chunk in enumerate(chunks):
        if (
            applied_chunks is not None
            and context.get("source_id")
            and extraction_chunk_key(
                context["source_id"],
                chunk,
                person_id=context.get("person_id", ""),
                source_url=source_url,
                model=model,
            )
            in applied_chunks
        ):
            count("extraction_cache_hits")
            metrics = getattr(client, "metrics", None)
            if metrics is not None:
                metrics.extraction_cache_hits += 1
            continue
        payloads.append(
            {
                "person": research_seed_payload(seed, purpose="extraction"),
                "source_url": source_url,
                "chunk_index": index,
                "content": chunk,
            }
        )
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
