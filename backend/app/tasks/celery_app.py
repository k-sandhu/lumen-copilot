"""Celery application + a trivial task.

The ``worker`` compose service runs ``celery -A app.tasks.celery_app worker``,
so this module must expose a ready-to-boot ``celery_app``. Broker and result
backend come from settings (``CELERY_BROKER_URL`` / ``CELERY_RESULT_BACKEND``,
both Redis) — read via ``core.config`` only, never ``os.environ`` here.

The ``ping`` task is a smoke task proving the worker round-trips; real tasks
(idempotent, retried, dead-lettered) land under CC-5.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "beacon",
    broker=_settings.celery_broker_url,
    backend=_settings.celery_result_backend,
)

# Conservative, explicit defaults. Serializers are JSON-only (no pickle).
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    enable_utc=True,
)


@celery_app.task(name="beacon.ping")  # type: ignore[misc]  # celery's task decorator is untyped
def ping() -> str:
    """Trivial liveness task — returns ``"pong"``. Proves the worker round-trips."""
    return "pong"
