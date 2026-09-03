"""
config — Centralised configuration and observability setup.

Exports:
    get_settings : Singleton application settings (reads env / .env, fails fast)
    bootstrap    : Call once per process — configures logging + validates the
                   environment is safe for APP_ENV
    get_logger   : Factory for structured loggers
"""

from config.logging import configure_logging, configure_logging_from_env, get_logger
from config.settings import (
    ConfigurationError,
    GitOpsSettings,
    Settings,
    assert_safe_for_environment,
    get_settings,
)

__all__ = [
    "ConfigurationError",
    "GitOpsSettings",
    "Settings",
    "assert_safe_for_environment",
    "bootstrap",
    "configure_logging",
    "configure_logging_from_env",
    "get_logger",
    "get_settings",
]


def bootstrap() -> None:
    """Process entry-point initialisation: configure logging, then fail fast if
    development-only configuration is active while ``APP_ENV=production``."""
    configure_logging_from_env()
    assert_safe_for_environment()
