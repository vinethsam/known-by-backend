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
from app.db.library import DatabaseResultFileStorage
from app.export.artifacts import build_export_artifact, export_media_type
from app.export.batch import BatchParseError, parse_batch
from app.export.naming import content_disposition
from app.library import LibraryLimitError, ResultFileStorage, SavedResultFile, SaveResultRequest
from app.logging import configure_logging
from app.processing import html_to_markdown
from app.retrieval.service import FetchConfigurationError, FetchError, FetchTimeoutError, StaticFetcher
from app.retrieval.urls import URLValidationError, validate_public_url
from app.schemas import Contract, JobCreated, JobResults, JobStatus, JobView, PersonSeed
from schemas import FetchRequest, ProcessedPage

logger = logging.getLogger(__name__)


class HealthResponse(Contract):
    status: str


class ReadyResponse(Contract):
    status: str
    checks: dict[str, bool]
    missing_configuration: list[str]


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    fetcher=None,
    library: ResultFileStorage | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or Store(settings)
    fetcher = fetcher or StaticFetcher(settings)
    library = library or DatabaseResultFileStorage(store.session_factory, settings)

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

    async def authorize_library(authorization: str | None = Header(default=None)):
        # Local deployments may omit auth for ordinary API debugging. Retained
        # shared files must never become public even in that configuration.
        if not settings.API_ACCESS_TOKEN or not settings.API_ACCESS_TOKEN.get_secret_value().strip():
            raise HTTPException(503, "Shared library requires API_ACCESS_TOKEN")
        await authorize(authorization)

    library_secured = [Depends(authorize_library)]

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

    async def job_export(job_id, format, provenance, *, require_completed=False):
        job = await get_job(job_id)
        if require_completed and job.status != JobStatus.completed:
            raise HTTPException(409, "Only completed research jobs can be saved to the library")
        results = await get_results(job_id)
        if require_completed and (
            results.status != JobStatus.completed
            or not results.people
            or any(person.result is None for person in results.people)
        ):
            raise HTTPException(409, "Completed result is unavailable")
        columns = await asyncio.to_thread(store.get_columns, str(job_id))
        return await asyncio.to_thread(
            build_export_artifact,
            results,
            columns,
            job,
            format,
            provenance=provenance,
            low_confidence_threshold=settings.SCORING.review_threshold,
        )

    @application.get("/v1/jobs/{job_id}/export", dependencies=secured)
    async def export(
        job_id: UUID,
        format: Literal["csv", "xlsx"] = Query(default="csv"),
        provenance: Literal["none", "field"] = Query(default="none"),
    ):
        artifact = await job_export(job_id, format, provenance)
        return Response(
            content=artifact.content,
            media_type=export_media_type(artifact.format),
            headers={"Content-Disposition": content_disposition(artifact.filename)},
        )

    @application.post(
        "/v1/jobs/{job_id}/library",
        response_model=SavedResultFile,
        status_code=201,
        dependencies=library_secured,
    )
    async def save_result(job_id: UUID, request: SaveResultRequest, response: Response):
        artifact = await job_export(job_id, request.format, request.provenance, require_completed=True)
        try:
            saved = await asyncio.to_thread(library.save, artifact)
        except LibraryLimitError as exc:
            raise HTTPException(413, str(exc)) from None
        response.headers["Location"] = f"/v1/library/files/{saved.file_id}"
        response.headers["Cache-Control"] = "private, no-store"
        return saved

    @application.get("/v1/library/files", response_model=list[SavedResultFile], dependencies=library_secured)
    async def list_saved_results(
        response: Response,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        response.headers["Cache-Control"] = "private, no-store"
        return await asyncio.to_thread(library.list_files, limit=limit, offset=offset)

    @application.get("/v1/library/files/{file_id}", dependencies=library_secured)
    async def download_saved_result(file_id: UUID):
        saved = await asyncio.to_thread(library.get, str(file_id))
        if saved is None:
            raise HTTPException(404, "Saved result not found")
        return Response(
            content=saved.content,
            media_type=export_media_type(saved.metadata.format),
            headers={
                "Content-Disposition": content_disposition(saved.metadata.filename),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.delete("/v1/library/files/{file_id}", status_code=204, dependencies=library_secured)
    async def delete_saved_result(file_id: UUID):
        if not await asyncio.to_thread(library.delete, str(file_id)):
            raise HTTPException(404, "Saved result not found")
        return Response(status_code=204, headers={"Cache-Control": "private, no-store"})

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
