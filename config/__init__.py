"""
config — Centralised configuration and observability setup.

Exports:
    settings  : Singleton application settings (reads from env / .env)
    get_logger: Factory for structured loggers
"""

from config.settings import Settings, get_settings
from config.logging import configure_logging, get_logger

__all__ = ["Settings", "get_settings", "configure_logging", "get_logger"]
