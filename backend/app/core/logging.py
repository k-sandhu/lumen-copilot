"""Structured logging via ``structlog``.

Ops observability — distinct from the product audit log (CC-8), which is a
separate sink. Configures ``structlog`` to emit JSON in non-local environments
and a human-friendly console renderer locally. Request/tenant correlation IDs
are bound by the middleware in ``app.main`` via ``contextvars`` so every log
line within a request carries them automatically.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

# Shared contextvar-backed map: middleware binds request/tenant ids here and
# every log event in that async context inherits them.
_merge_contextvars = structlog.contextvars.merge_contextvars


def configure_logging(*, log_level: str = "info", json_logs: bool = True) -> None:
    """Configure ``structlog`` + stdlib logging for the whole process.

    Idempotent: safe to call once at startup (lifespan) and again in tests.

    Args:
        log_level: Minimum level name (e.g. ``"info"``, ``"debug"``).
        json_logs: Emit JSON (production/ops) vs. a console renderer (local).
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    shared_processors: list[structlog.types.Processor] = [
        _merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, etc.) through a basic handler at
    # the same level so third-party logs aren't lost.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
