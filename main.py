"""Compatibility ASGI entrypoint: uvicorn main:app."""

from app.main import app

__all__ = ["app"]
