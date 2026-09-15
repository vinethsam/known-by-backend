"""ASGI entrypoint: uvicorn app.main:app."""

from app.api.routes import create_app

app = create_app()
