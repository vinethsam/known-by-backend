"""FastAPI endpoint wiring for retrieval and preprocessing."""

from fastapi import FastAPI, HTTPException, status

from fetcher import (
    FetchConfigurationError,
    FetchError,
    FetchTimeoutError,
    fetch_url,
)
from processing import html_to_markdown
from schemas import FetchRequest, ProcessedPage


app = FastAPI()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/process", response_model=ProcessedPage)
async def process_page(request: FetchRequest) -> ProcessedPage:
    try:
        fetched_page = await fetch_url(str(request.url))
    except FetchConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except FetchTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except FetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    markdown = html_to_markdown(
        fetched_page.html,
        base_url=str(fetched_page.final_url),
    )

    return ProcessedPage(
        requested_url=fetched_page.requested_url,
        final_url=fetched_page.final_url,
        status=fetched_page.status,
        content_type=fetched_page.content_type,
        markdown=markdown,
    )
