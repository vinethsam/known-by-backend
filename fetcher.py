"""Compatibility static-fetch entrypoint; the Worker contract is unchanged."""

from app.config import get_settings
from app.retrieval.service import FetchConfigurationError, FetchError, FetchTimeoutError, StaticFetcher
from schemas import FetchResult


async def fetch_url(url: str) -> FetchResult:
    fetcher = StaticFetcher(get_settings())
    try:
        page = await fetcher.fetch(url)
        return FetchResult(
            **page.model_dump(include={"requested_url", "final_url", "status", "content_type", "html"})
        )
    finally:
        await fetcher.close()


__all__ = ["fetch_url", "FetchError", "FetchTimeoutError", "FetchConfigurationError"]
