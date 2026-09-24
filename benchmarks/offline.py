"""Deterministic HTTP fixtures exercising real research, leases and persistence.

Run ``python -m benchmarks.offline --output report.json``. No live mode exists:
both outbound clients use MockTransport and DNS resolves only to a public fixture
address. Production limits come from Settings; only fixture service addresses and
the temporary SQLite database differ from normal worker execution.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import socket
import tempfile
import threading
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import httpx
from alembic.config import Config
from sqlalchemy import event

from alembic import command
from app.config import Settings
from app.db import Store
from app.providers.openrouter import OpenRouterClient
from app.providers.search import OpenRouterSearchProvider
from app.research.orchestrator import ResearchOrchestrator
from app.retrieval.service import RetrievalService, StaticFetcher
from app.schemas import JobResults, PersonSeed
from app.worker import process_lease

URLS = ("https://employer.example.org/directory", "https://university.example.edu/directory")
FIXTURE_VERSION = 1
NORMALIZATION_VERSION = 3


def evidence(name: str) -> str:
    return (
        f"{name} works for Example Foundation in Ghana as Programme Director. "
        f"{name} earned BSc in Economics at Example University."
    )


def normalized_results(results: JobResults) -> dict:
    """Keep decisions, claims and source provenance; remove operational identities.

    IDs become content-derived references, so out-of-order completion cannot hide
    changed evidence, confidence, alternatives, review reasons or terminal state.
    Usage/timings and retained processing buffers are measured separately.
    """
    people = []
    for view in results.people:
        if view.result is None:
            raise AssertionError("Benchmark person has no durable result")
        result = view.result
        references = {source.source_id: source.canonical_url for source in result.sources}
        representatives = {}
        claims = []
        for claim in result.claims:
            payload = claim.model_dump(
                mode="json", exclude={"claim_id", "person_id", "created_at", "prompt_version"}
            )
            payload["source_id"] = references[claim.source_id]
            references[claim.claim_id] = hashlib.sha256(_json(payload).encode()).hexdigest()
            representatives[claim.claim_id] = hashlib.sha256(
                _json({key: value for key, value in payload.items() if key != "source_id"}).encode()
            ).hexdigest()
            claims.append(payload)

        def normalize(value):
            if isinstance(value, str):
                return references.get(value, value)
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            if isinstance(value, list):
                return sorted((normalize(item) for item in value), key=_json)
            return value

        profile = result.profile.model_dump(
            mode="json", exclude={"person_id", "started_at", "completed_at", "metrics"}
        )
        # Equal-scoring copies of one fact can choose a different UUID as their
        # representative. Apply the same normalization to the legacy projection
        # and every education-specific record. All supporting source-linked claims
        # remain compared by the recursive source/claim reference normalization.
        decision_sets = [profile["fields"]]
        decision_sets.extend(record["fields"] for record in profile.get("records", []))
        for fields in decision_sets:
            for decision in fields.values():
                selected = decision["selected_claim_id"]
                decision["selected_claim_id"] = representatives.get(selected, selected)
        sources = [
            source.model_dump(
                mode="json",
                exclude={"source_id", "person_id", "retrieved_at", "compacted_text", "raw_content"},
            )
            for source in result.sources
        ]
        people.append(
            normalize(
                {
                    "status": view.status,
                    "error_code": view.error_code,
                    "profile": profile,
                    "sources": sources,
                    "claims": claims,
                }
            )
        )
    return {"status": results.status, "people": people}


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Counters:
    def __init__(self):
        self.counts = Counter()
        self.active = Counter()
        self.peak = Counter()
        self.lock = threading.Lock()

    def add(self, key):
        with self.lock:
            self.counts[key] += 1

    @asynccontextmanager
    async def operation(self, key):
        self.add(key)
        self.active[key] += 1
        self.peak[key] = max(self.peak[key], self.active[key])
        try:
            yield
        finally:
            self.active[key] -= 1


async def run_batch(people_count: int, directory: Path, *, latency_ms: float = 2) -> dict:
    if (directory / "benchmark.db").exists():
        raise ValueError("Benchmark requires a fresh database path")
    settings = Settings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=f"sqlite:///{directory / 'benchmark.db'}",
        OPENROUTER_API_KEY="offline-fixture",
        OPENROUTER_SOURCE_MODEL="fixture/source",
        OPENROUTER_EXTRACTION_MODEL="fixture/extraction",
        STATIC_FETCH_WORKER_URL="https://worker.example.net/fetch",
        STATIC_FETCH_WORKER_SECRET="offline-fixture",
        PLAYWRIGHT_ENABLED=False,
    )
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    command.upgrade(config, "head")
    counters = Counters()
    performance_events = []

    class PerformanceCapture(logging.Handler):
        def emit(self, record):
            if record.msg == "person_performance":
                performance_events.append(dict(record.performance))

    performance_logger = logging.getLogger("app.research.telemetry")
    capture = PerformanceCapture()
    previous_level = performance_logger.level
    previous_disabled = performance_logger.disabled
    previous_propagate = performance_logger.propagate
    store = Store(settings)
    original_cache_get = store.get_cache

    def cache_get(job_id, url):
        value = original_cache_get(job_id, url)
        counters.add("cache_hits" if value is not None else "cache_misses")
        return value

    store.get_cache = cache_get

    def statement(_connection, _cursor, sql, _parameters, _context, _executemany):
        counters.add("db_statements")
        counters.add("db_" + sql.lstrip().split(maxsplit=1)[0].lower())

    seeds = [
        PersonSeed(full_name=f"Person {index:03d}", organisation="Example Foundation", country="Ghana")
        for index in range(people_count)
    ]
    body = "".join(f"<p>{evidence(seed.full_name)}</p>" for seed in seeds)

    async def static(request):
        assert str(request.url) == settings.STATIC_FETCH_WORKER_URL
        target = json.loads(request.content)["url"]
        assert target in URLS
        async with counters.operation("fetches"):
            await asyncio.sleep(latency_ms / 1000)
            return httpx.Response(
                200,
                json={
                    "requested_url": target,
                    "final_url": target,
                    "status": 200,
                    "content_type": "text/html",
                    "html": f"<title>Staff directory</title><h1>{target}</h1>{body}",
                },
            )

    async def model(request):
        assert request.url.host == "openrouter.ai"
        payload = json.loads(request.content)
        data = {} if payload.get("tools") else json.loads(payload["messages"][1]["content"])["payload"]
        operation = (
            "searches"
            if payload.get("tools")
            else {"validate_candidates": "validations", "plan_search_queries": "planning"}.get(
                data.get("task"), "extractions"
            )
        )
        async with counters.operation("models"), counters.operation(operation):
            await asyncio.sleep(latency_ms / 1000)
            if payload.get("tools"):
                message = {
                    "content": "Directory citations",
                    "annotations": [
                        {"type": "url_citation", "url_citation": {"url": url, "title": "Staff directory"}}
                        for url in URLS
                    ],
                }
            else:
                if data.get("task") == "validate_candidates":
                    answer = {
                        "decisions": [
                            {
                                "candidate_id": c["candidate_id"],
                                "source_type": "employer",
                                "relevance": "likely",
                            }
                            for c in data["candidates"]
                        ]
                    }
                elif data.get("task") == "plan_search_queries":
                    answer = {"queries": []}
                else:
                    name = data["person"]["full_name"]
                    values = dict(
                        full_name=name,
                        organisation="Example Foundation",
                        job_title="Programme Director",
                        university_name="Example University",
                        degree_type="BSc",
                        subject="Economics",
                    )
                    answer = {
                        "claims": [
                            {
                                "field": key,
                                "value": value,
                                "evidence": evidence(name),
                                "subject_name": name,
                                "fact_group": "education"
                                if key in {"university_name", "degree_type", "subject"}
                                else "employment",
                            }
                            for key, value in values.items()
                        ]
                        if evidence(name) in data["content"]
                        else []
                    }
                message = {"content": json.dumps(answer)}
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": message}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "cost": 0.0002,
                        **({"server_tool_use": {"web_search_requests": 1}} if payload.get("tools") else {}),
                    },
                },
            )

    async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as model_http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(static)) as static_http:
            model_client = OpenRouterClient(settings, model_http)
            retrieval = RetrievalService(settings, StaticFetcher(settings, static_http), cache=store)
            pipeline = ResearchOrchestrator(
                settings, OpenRouterSearchProvider(settings, model_client), model_client, retrieval
            )

            async def consume():
                while lease := await asyncio.to_thread(store.claim_task, "offline-benchmark"):
                    async with counters.operation("people"):
                        await process_lease(store, pipeline, lease, settings)

            event.listen(store.engine, "before_cursor_execute", statement)
            performance_logger.addHandler(capture)
            performance_logger.setLevel(logging.INFO)
            performance_logger.disabled = False
            performance_logger.propagate = False
            started = perf_counter()
            try:
                job = store.create_job(seeds)
                with patch(
                    "socket.getaddrinfo",
                    return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
                ):
                    await asyncio.gather(*(consume() for _ in range(settings.MAX_CONCURRENT_PEOPLE)))
                elapsed_ms = (perf_counter() - started) * 1000
                count_before_results = counters.counts["db_statements"]
                results = store.get_results(job.job_id)
                result_queries = counters.counts["db_statements"] - count_before_results
                normalized = normalized_results(results)
            finally:
                event.remove(store.engine, "before_cursor_execute", statement)
                performance_logger.removeHandler(capture)
                performance_logger.setLevel(previous_level)
                performance_logger.disabled = previous_disabled
                performance_logger.propagate = previous_propagate
                await retrieval.close()
                store.engine.dispose()
    cache_lookups = counters.counts["cache_hits"] + counters.counts["cache_misses"]
    return {
        "people": people_count,
        "elapsed_ms": round(elapsed_ms, 2),
        "average_ms_per_person": round(elapsed_ms / people_count, 2),
        "counts": dict(counters.counts),
        "peak_concurrency": dict(counters.peak),
        "result_read_queries": result_queries,
        "cache_hit_rate": round(counters.counts["cache_hits"] / max(1, cache_lookups), 4),
        "searches_per_person": counters.counts["searches"] / people_count,
        "fetches_per_person": counters.counts["fetches"] / people_count,
        "result_sha256": hashlib.sha256(_json(normalized).encode()).hexdigest(),
        "normalized_results": normalized,
        "performance_events": len(performance_events),
        "performance_totals": {
            key: round(sum(item[key] for item in performance_events), 8 if key == "provider_cost" else 3)
            for key in performance_events[0]
        }
        if performance_events
        else {},
    }


async def benchmark(sizes=(1, 5, 25, 100), *, latency_ms=2):
    reports = []
    for size in sizes:
        with tempfile.TemporaryDirectory(prefix="knownby-offline-") as path:
            reports.append(await run_batch(size, Path(path), latency_ms=latency_ms))
    return {
        "fixture_version": FIXTURE_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "mock_latency_ms": latency_ms,
        "batches": reports,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1, 5, 25, 100])
    parser.add_argument("--latency-ms", type=float, default=2)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    if any(size < 1 or size > 100 for size in args.sizes) or args.latency_ms < 0:
        parser.error("Sizes must be 1..100 and latency must be nonnegative")
    report = asyncio.run(benchmark(args.sizes, latency_ms=args.latency_ms))
    if args.compare:
        prior = json.loads(args.compare.read_text(encoding="utf-8"))
        if any(prior.get(key) != report[key] for key in ("fixture_version", "normalization_version")):
            raise SystemExit("Comparison requires the same fixture and normalization versions")
        hashes = {batch["people"]: batch["result_sha256"] for batch in prior["batches"]}
        report["outputs_equivalent"] = all(
            hashes.get(batch["people"]) == batch["result_sha256"] for batch in report["batches"]
        )
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                **report,
                "batches": [
                    {key: value for key, value in batch.items() if key != "normalized_results"}
                    for batch in report["batches"]
                ],
            },
            indent=2,
        )
    )
    if report.get("outputs_equivalent") is False:
        raise SystemExit("Normalized research outputs differ from comparison report")


if __name__ == "__main__":
    main()
