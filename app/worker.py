"""Separate durable worker process with leases, renewal, cancellation and recovery."""

from __future__ import annotations

import asyncio
import logging
import signal
from uuid import uuid4

from app.config import get_settings
from app.db import Store
from app.logging import configure_logging
from app.providers.openrouter import OpenRouterClient
from app.providers.search import OpenRouterSearchProvider
from app.research.orchestrator import ResearchOrchestrator
from app.retrieval.service import RetrievalService

logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    pass


async def process_lease(store, orchestrator, lease, settings):
    async def checkpoint(result):
        saved = await asyncio.to_thread(store.checkpoint, lease, result)
        if not saved:
            raise LeaseLost()

    async def work():
        async with asyncio.timeout(settings.PERSON_TIMEOUT_SECONDS):
            result = await orchestrator.research(lease.job_id, lease.person_id, lease.seed, checkpoint)
        if result.profile.coverage == 0 and result.profile.metrics.error_codes:
            await asyncio.to_thread(store.fail_task, lease, "RESEARCH_PROVIDERS_FAILED")
        else:
            await asyncio.to_thread(store.finish_task, lease, result)

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
        await asyncio.to_thread(store.fail_task, lease, "PERSON_TIMEOUT")
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception:
        logger.error(
            "person_failed",
            exc_info=True,
            extra={"job_id": lease.job_id, "person_id": lease.person_id, "error_code": "RESEARCH_FAILED"},
        )
        await asyncio.to_thread(store.fail_task, lease, "RESEARCH_FAILED")


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
    try:
        while not stop_event.is_set():
            await asyncio.to_thread(store.heartbeat, worker_id)
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
