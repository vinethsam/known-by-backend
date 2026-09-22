"""Separate durable worker process with leases, renewal, cancellation and recovery."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import timezone
from time import monotonic
from uuid import uuid4

from app.config import get_settings
from app.db import Store
from app.logging import configure_logging
from app.providers.openrouter import OpenRouterClient
from app.providers.search import OpenRouterSearchProvider
from app.research.orchestrator import ResearchOrchestrator
from app.research.telemetry import person_performance, stage
from app.retrieval.service import RetrievalService
from app.schemas import utcnow

logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    pass


FATAL_RESEARCH_ERROR_CODES = (
    "SOURCE_ADVISOR_VALIDATION_ERROR",
    "SOURCE_ADVISOR_PROVIDER_ERROR",
    "EXTRACTION_PROVIDER_FAILED",
    "OPENROUTER_HTTP_ERROR",
    "OPENROUTER_PROVIDER_ERROR",
    "OPENROUTER_TRANSPORT_ERROR",
    "OPENROUTER_TIMEOUT",
    "OPENROUTER_SCHEMA_ERROR",
    "OPENROUTER_RESPONSE_TOO_LARGE",
    # Preserve classification for checkpoints created by older workers.
    "SEARCH_PROVIDER_FAILED",
    "SOURCE_PROVIDER_FAILED",
    "SOURCE_PLANNING_FAILED",
)


def terminal_research_error(error_codes: list[str]) -> str | None:
    present = set(error_codes)
    if not present:
        return None
    return next(
        (code for code in FATAL_RESEARCH_ERROR_CODES if code in present),
        sorted(present)[0],
    )


async def process_lease(store, orchestrator, lease, settings):
    created = getattr(lease, "created_at", None)
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    queue_wait = (utcnow() - created).total_seconds() * 1000 if created is not None else 0
    with person_performance(lease.job_id, lease.person_id, queue_wait_ms=queue_wait):
        await _process_lease(store, orchestrator, lease, settings)


async def _process_lease(store, orchestrator, lease, settings):
    async def persist(method, *args):
        with stage("persistence_ms"):
            return await asyncio.to_thread(method, *args)

    async def checkpoint(result):
        saved = await persist(store.checkpoint, lease, result)
        if not saved:
            raise LeaseLost()

    async def work():
        async with asyncio.timeout(settings.PERSON_TIMEOUT_SECONDS):
            result = await orchestrator.research(lease.job_id, lease.person_id, lease.seed, checkpoint)
        fatal_error = terminal_research_error(result.profile.metrics.error_codes)
        if result.profile.coverage == 0 and fatal_error:
            await persist(store.fail_task, lease, fatal_error)
        else:
            await persist(store.finish_task, lease, result)

    task = asyncio.create_task(work())
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=settings.WORKER_HEARTBEAT_SECONDS)
            if done:
                break
            if not await asyncio.to_thread(store.renew_lease, lease):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
        await task
    except LeaseLost:
        logger.info("lease_lost", extra={"job_id": lease.job_id, "person_id": lease.person_id})
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await persist(store.fail_task, lease, "PERSON_TIMEOUT")
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception:
        # A lease-renewal/database error can occur while research is still live.
        # Stop owned network/model work before recording the terminal failure.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.error(
            "person_failed",
            exc_info=True,
            extra={"job_id": lease.job_id, "person_id": lease.person_id, "error_code": "RESEARCH_FAILED"},
        )
        await persist(store.fail_task, lease, "RESEARCH_FAILED")
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def run_worker(settings=None, store=None, orchestrator=None, stop_event=None):
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL)
    store = store or Store(settings)
    if not await asyncio.to_thread(store.ready):
        raise RuntimeError("Database is unavailable or migrations are missing")
    if orchestrator is None and settings.missing_services():
        raise RuntimeError("Missing service settings: " + ", ".join(settings.missing_services()))
    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    resources = None
    if orchestrator is None:
        model = OpenRouterClient(settings)
        search = OpenRouterSearchProvider(settings, model)
        retrieval = RetrievalService(settings, cache=store)
        resources = (model, retrieval)
        orchestrator = ResearchOrchestrator(settings, search, model, retrieval)
    worker_id = str(uuid4())
    active = set()
    next_heartbeat = 0.0
    try:
        while not stop_event.is_set():
            if monotonic() >= next_heartbeat:
                await asyncio.to_thread(store.heartbeat, worker_id)
                next_heartbeat = monotonic() + settings.WORKER_HEARTBEAT_SECONDS
            completed = {task for task in active if task.done()}
            if completed:
                await asyncio.gather(*completed)
                active.difference_update(completed)
            while len(active) < settings.MAX_CONCURRENT_PEOPLE:
                lease = await asyncio.to_thread(store.claim_task, worker_id)
                if lease is None:
                    break
                active.add(asyncio.create_task(process_lease(store, orchestrator, lease, settings)))
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=min(settings.WORKER_POLL_SECONDS, settings.WORKER_HEARTBEAT_SECONDS),
                )
            except TimeoutError:
                pass
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if resources:
            await resources[0].aclose()
            await resources[1].close()
        store.engine.dispose()


def main():
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
