"""
config — Centralised configuration and observability setup.

Exports:
    settings  : Singleton application settings (reads from env / .env)
    get_logger: Factory for structured loggers
"""

from config.logging import configure_logging, get_logger
from config.settings import GitOpsSettings, Settings, get_settings

__all__ = ["GitOpsSettings", "Settings", "get_settings", "configure_logging", "get_logger"]
