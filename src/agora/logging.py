"""Structured JSON logging setup.

Every log record includes ``saga_id``, ``step``, ``actor``, and
``idempotency_key`` when bound through ``structlog`` contextvars.
"""

from __future__ import annotations

import logging
import sys

import structlog

from agora.config import get_settings


def configure_logging() -> None:
    """Initialise structlog + stdlib logging with JSON output.

    Idempotent: safe to call multiple times.
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger; configures logging on first call."""
    if not structlog.is_configured():
        configure_logging()
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
