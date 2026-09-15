"""Compatibility import for the central environment configuration."""

from app.config import Settings, get_settings

settings = get_settings()
__all__ = ["Settings", "settings"]
