"""Separate durable worker process with leases, renewal, cancellation and recovery."""

from __future__ import annotations

import asyncio
import logging
import random
import signal
from datetime import timezone
from time import monotonic
from uuid import uuid4

from app.config import get_settings
from app.db import Store
from app.db.store import is_transient_database_error
from app.logging import configure_logging
from app.providers.openrouter import OpenRouterClient
from app.providers.search import OpenRouterSearchProvider
from app.research.orchestrator import RecoverableResearchError, ResearchOrchestrator
from app.research.telemetry import person_performance, stage
from app.retrieval.service import RetrievalService
from app.schemas import PersonStatus, utcnow

logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    pass


class ResearchClock:
    """Person time limits count active work, excluding database recovery waits."""

    def __init__(self):
        self.started = monotonic()
        self.waiters = 0
        self.paused_at = None
        self.paused = 0.0

    def pause(self):
        if not self.waiters:
            self.paused_at = monotonic()
        self.waiters += 1

    def resume(self):
        self.waiters -= 1
        if not self.waiters:
            self.paused += monotonic() - self.paused_at
            self.paused_at = None

    def elapsed(self):
        return (self.paused_at or monotonic()) - self.started - self.paused


async def retry_database(store, method, *args, clock=None, stop_event=None, **kwargs):
    """Each Store call creates a fresh transaction; only connectivity errors retry."""
    attempt, last_log = 0, 0.0
    paused = False
    operation = getattr(method, "__name__", "database_operation")
    context = {"operation": operation}
    if args and hasattr(args[0], "job_id"):
        context.update(job_id=args[0].job_id, person_id=args[0].person_id)
    try:
        while True:
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError()
            if stop_event is not None and stop_event.is_set():
                raise asyncio.CancelledError()
            try:
                value = await asyncio.to_thread(method, *args, **kwargs)
                if attempt:
                    logger.info(
                        "database_connectivity_restored",
                        extra=dict(context, retry_attempt=attempt),
                    )
                return value
            except Exception as exc:
                if not is_transient_database_error(exc):
                    raise
                if not paused and clock is not None:
                    clock.pause()
                    paused = True
                if not attempt or monotonic() - last_log >= 60:
                    logger.warning(
                        "database_connectivity_lost" if not attempt else "database_reconnection_attempt",
                        extra=dict(
                            context,
                            error_code="DATABASE_UNAVAILABLE",
                            exception_type=type(exc).__name__,
                            retry_attempt=attempt,
                        ),
                    )
                    last_log = monotonic()
                invalidate = getattr(store, "invalidate_connections", None)
                if invalidate:
                    await asyncio.to_thread(invalidate)
                delay = min(30.0, 0.5 * 2 ** min(attempt, 6)) * random.uniform(0.8, 1.0)
                attempt += 1
                if stop_event is None:
                    await asyncio.sleep(delay)
                else:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
    finally:
        if paused:
            clock.resume()


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
    if getattr(lease, "checkpoint", None) is not None:
        logger.info("research_resumed", extra={"job_id": lease.job_id, "person_id": lease.person_id})
    created = getattr(lease, "created_at", None)
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    queue_wait = (utcnow() - created).total_seconds() * 1000 if created is not None else 0
    with person_performance(lease.job_id, lease.person_id, queue_wait_ms=queue_wait):
        await _process_lease(store, orchestrator, lease, settings)


async def _process_lease(store, orchestrator, lease, settings):
    clock = ResearchClock()
    latest = getattr(lease, "checkpoint", None)

    async def persist(method, *args):
        with stage("persistence_ms"):
            return await retry_database(store, method, *args, clock=clock)

    async def checkpoint(result):
        nonlocal latest
        latest = result.model_copy(deep=True)
        saved = await persist(store.checkpoint, lease, result)
        if not saved:
            raise LeaseLost()

    async def work():
        if isinstance(orchestrator, ResearchOrchestrator):
            result = await orchestrator.research(
                lease.job_id,
                lease.person_id,
                lease.seed,
                checkpoint,
                resume=getattr(lease, "checkpoint", None),
            )
        else:
            result = await orchestrator.research(lease.job_id, lease.person_id, lease.seed, checkpoint)
        if result.profile.coverage == 0:
            failure_reason = (
                result.profile.metrics.failure_reason
                or terminal_research_error(result.profile.metrics.error_codes)
                or "INSUFFICIENT_EVIDENCE"
            )
            result.profile.status = PersonStatus.failed
            for record in result.profile.records:
                record.status = PersonStatus.failed
            if not await persist(store.fail_task, lease, failure_reason, result):
                raise LeaseLost()
        else:
            if not await persist(store.finish_task, lease, result):
                raise LeaseLost()

    task = asyncio.create_task(work())
    try:
        while not task.done():
            remaining = settings.PERSON_TIMEOUT_SECONDS - clock.elapsed()
            if remaining <= 0 and not clock.waiters:
                raise TimeoutError()
            done, _ = await asyncio.wait(
                {task}, timeout=min(settings.WORKER_HEARTBEAT_SECONDS, max(0.05, remaining))
            )
            if done:
                break
            if not await persist(store.renew_lease, lease):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
        await task
    except LeaseLost:
        logger.info("lease_lost", extra={"job_id": lease.job_id, "person_id": lease.person_id})
    except RecoverableResearchError as exc:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        outage_attempt = 0
        if latest is not None:
            outage_attempt = latest.profile.metrics.recovery.get("outage_attempt", 0)
            latest.profile.metrics.recovery["outage_attempt"] = outage_attempt + 1
        delay = max(exc.retry_after_seconds or 0, min(30.0, 5 * 2 ** min(outage_attempt, 3)))
        saved = await persist(store.defer_task, lease, exc.error_code, delay, latest)
        logger.info(
            "job_waiting_for_infrastructure" if saved else "lease_lost",
            extra={
                "job_id": lease.job_id,
                "person_id": lease.person_id,
                "error_code": exc.error_code,
                "retry_after_seconds": delay,
            },
        )
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await persist(store.fail_task, lease, "PERSON_TIMEOUT")
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    except Exception as exc:
        # A lease-renewal/database error can occur while research is still live.
        # Stop owned network/model work before recording the terminal failure.
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        logger.error(
            "person_failed",
            extra={
                "job_id": lease.job_id,
                "person_id": lease.person_id,
                "error_code": "RESEARCH_FAILED",
                "exception_type": type(exc).__name__,
            },
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
        if not await retry_database(store, store.ready, raise_transient=True, stop_event=stop_event):
            raise RuntimeError("Database configuration is invalid or migrations are missing")
        while not stop_event.is_set():
            if monotonic() >= next_heartbeat:
                await retry_database(store, store.heartbeat, worker_id, stop_event=stop_event)
                next_heartbeat = monotonic() + settings.WORKER_HEARTBEAT_SECONDS
            completed = {task for task in active if task.done()}
            if completed:
                outcomes = await asyncio.gather(*completed, return_exceptions=True)
                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        logger.error(
                            "person_task_interrupted", extra={"exception_type": type(outcome).__name__}
                        )
                active.difference_update(completed)
            while len(active) < settings.MAX_CONCURRENT_PEOPLE:
                lease = await retry_database(
                    store, store.claim_task, worker_id, str(uuid4()), stop_event=stop_event
                )
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
    except asyncio.CancelledError:
        if not stop_event.is_set():
            raise
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
