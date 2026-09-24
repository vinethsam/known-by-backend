"""API factory; paid research runs only in the separate worker process."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.api.limits import RequestSizeLimitMiddleware
from app.config import Settings, get_settings
from app.db import Store
from app.export.batch import BatchParseError, parse_batch
from app.export.formats import export_results
from app.logging import configure_logging
from app.processing import html_to_markdown
from app.retrieval.service import FetchConfigurationError, FetchError, FetchTimeoutError, StaticFetcher
from app.retrieval.urls import URLValidationError, validate_public_url
from app.schemas import Contract, JobCreated, JobResults, JobView, PersonSeed
from schemas import FetchRequest, ProcessedPage

logger = logging.getLogger(__name__)


class HealthResponse(Contract):
    status: str


class ReadyResponse(Contract):
    status: str
    checks: dict[str, bool]
    missing_configuration: list[str]


def create_app(settings: Settings | None = None, store: Store | None = None, fetcher=None) -> FastAPI:
    settings = settings or get_settings()
    store = store or Store(settings)
    fetcher = fetcher or StaticFetcher(settings)

    @asynccontextmanager
    async def lifespan(application):
        configure_logging(settings.LOG_LEVEL)
        yield
        await fetcher.close()
        store.engine.dispose()

    application = FastAPI(title="People Research API", version="1.0.0", lifespan=lifespan)
    application.add_middleware(RequestSizeLimitMiddleware, limit=settings.MAX_UPLOAD_BYTES + 65536)
    application.state.settings, application.state.store = settings, store

    async def authorize(authorization: str | None = Header(default=None)):
        if settings.API_ACCESS_TOKEN:
            expected = "Bearer " + settings.API_ACCESS_TOKEN.get_secret_value()
            if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
                raise HTTPException(
                    401, "Invalid or missing API access token", headers={"WWW-Authenticate": "Bearer"}
                )

    secured = [Depends(authorize)]

    async def accept_jobs():
        if not await asyncio.to_thread(store.ready):
            raise HTTPException(503, "Database is unavailable or migrations are missing")
        missing = settings.missing_services()
        if missing:
            raise HTTPException(503, {"code": "SERVICE_CONFIGURATION_MISSING", "settings": missing})

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
                    for error in exc.errors()
                ]
            },
        )

    @application.exception_handler(Exception)
    async def internal_error(request, exc):
        logger.error("api_request_failed", extra={"error_code": type(exc).__name__})
        return JSONResponse(status_code=500, content={"detail": "Internal service error"})

    @application.get("/health", response_model=HealthResponse)
    async def health():
        return HealthResponse(status="ok")

    @application.get("/ready", response_model=ReadyResponse)
    async def ready():
        database = await asyncio.to_thread(store.ready)
        worker = await asyncio.to_thread(store.worker_is_alive) if database else False
        missing = settings.missing_services()
        browser = True
        if settings.PLAYWRIGHT_ENABLED:
            from pathlib import Path

            from playwright.async_api import async_playwright

            try:
                async with async_playwright() as pw:
                    browser = Path(pw.chromium.executable_path).is_file()
            except Exception:
                browser = False
        checks = {
            "database": database,
            "configuration": not missing,
            "worker": worker,
            "browser_installed": browser,
        }
        body = ReadyResponse(
            status="ready" if all(checks.values()) else "not_ready",
            checks=checks,
            missing_configuration=missing,
        )
        return JSONResponse(status_code=200 if all(checks.values()) else 503, content=body.model_dump())

    @application.post("/v1/research/person", response_model=JobCreated, status_code=202, dependencies=secured)
    async def submit_person(seed: PersonSeed):
        await accept_jobs()
        for url in seed.preferred_urls:
            try:
                await validate_public_url(str(url), settings)
            except URLValidationError:
                raise HTTPException(422, "A preferred URL is unsafe") from None
        return await asyncio.to_thread(
            store.create_job, [seed], [{"full_name": seed.full_name}], ["full_name"]
        )

    @application.post("/v1/research/batch", response_model=JobCreated, status_code=202, dependencies=secured)
    async def submit_batch(file: UploadFile = File(...), name_column: str | None = Form(default=None)):
        await accept_jobs()
        try:
            content = await file.read(settings.MAX_UPLOAD_BYTES + 1)
            if len(content) > settings.MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Batch upload exceeds maximum size")
            batch = await asyncio.to_thread(
                parse_batch, content, file.filename or "batch.csv", name_column, settings
            )
        except BatchParseError as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            await file.close()
        return await asyncio.to_thread(store.create_job, batch.seeds, batch.rows, batch.columns)

    @application.get("/v1/jobs/{job_id}", response_model=JobView, dependencies=secured)
    async def get_job(job_id: UUID):
        job = await asyncio.to_thread(store.get_job, str(job_id))
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @application.get("/v1/jobs/{job_id}/results", response_model=JobResults, dependencies=secured)
    async def get_results(job_id: UUID):
        results = await asyncio.to_thread(store.get_results, str(job_id))
        if results is None:
            raise HTTPException(404, "Job not found")
        return results

    @application.post("/v1/jobs/{job_id}/cancel", response_model=JobView, dependencies=secured)
    async def cancel(job_id: UUID):
        if not await asyncio.to_thread(store.cancel_job, str(job_id)):
            await get_job(job_id)
            raise HTTPException(409, "Job is already finished")
        return await get_job(job_id)

    @application.get("/v1/jobs/{job_id}/export", dependencies=secured)
    async def export(
        job_id: UUID,
        format: Literal["csv", "xlsx"] = Query(default="csv"),
        provenance: Literal["none", "field"] = Query(default="none"),
    ):
        results = await get_results(job_id)
        columns = await asyncio.to_thread(store.get_columns, str(job_id))
        data = await asyncio.to_thread(export_results, results, columns, format, provenance=provenance)
        mime = (
            "text/csv; charset=utf-8"
            if format == "csv"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return Response(
            content=data,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="research-{job_id}.{format}"'},
        )

    @application.post("/process", response_model=ProcessedPage, dependencies=secured, tags=["legacy-debug"])
    async def process_page(request: FetchRequest):
        try:
            page = await fetcher.fetch(str(request.url))
        except FetchConfigurationError as exc:
            raise HTTPException(503, str(exc)) from None
        except FetchTimeoutError as exc:
            raise HTTPException(504, str(exc)) from None
        except FetchError as exc:
            raise HTTPException(502, str(exc)) from None
        return ProcessedPage(
            requested_url=page.requested_url,
            final_url=page.final_url,
            status=page.status,
            content_type=page.content_type,
            markdown=html_to_markdown(page.html, page.final_url),
        )

    return application
