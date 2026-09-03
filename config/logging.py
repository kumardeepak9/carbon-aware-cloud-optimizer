"""
config/logging.py — Structured logging configuration using structlog.

All application modules should obtain their logger via::

    from config import get_logger
    log = get_logger(__name__)

In production (LOG_FORMAT=json) logs are emitted as single-line JSON objects
compatible with Loki / Cloud Logging / ELK.

In development (LOG_FORMAT=pretty) logs are rendered with colour and indentation
for human readability.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """
    Initialise structlog with the requested level and format.

    Call once at application startup (e.g. in ``agent.py`` or ``report.py``).

    Args:
        level: Python logging level string (DEBUG, INFO, WARNING, ERROR).
        fmt:   Output format — "json" for production, "pretty" for development.
    """
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if fmt == "pretty":
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def configure_logging_from_env() -> None:
    """Configure logging from ``LOG_LEVEL`` / ``LOG_FORMAT`` (via ``AppSettings``).

    Call once at the top of every process entry point. Without it, ``LOG_FORMAT``
    is read from the environment but never applied, so a production deployment
    silently keeps structlog's default (non-JSON) renderer.
    """
    from config.settings import AppSettings

    app = AppSettings()
    configure_logging(level=app.log_level, fmt=app.log_format)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Return a named, bound structlog logger.

    Args:
        name: Typically ``__name__`` of the calling module.

    Returns:
        A structlog BoundLogger instance.
    """
    return structlog.get_logger(name)
