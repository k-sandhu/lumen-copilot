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
    "lumen_copilot",
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
    # Modules the worker imports to register tasks. Explicit so the worker
    # entrypoint (``-A app.tasks.celery_app``) discovers the ingestion task (#21),
    # the connector sync task (#20), the artifact-retention janitor (#208), and the
    # headless agent-run task (#235) regardless of package ``__init__`` side effects.
    imports=(
        "app.tasks.ingest",
        "app.tasks.sync_source",
        "app.tasks.artifact_retention",
        "app.tasks.run_assistant",
    ),
    # Fail fast when publishing to an unreachable broker rather than looping
    # through a long reconnect cycle: a producer (the API after-commit enqueue)
    # must not block the response on a transient broker outage. The single
    # publish attempt raises ``kombu.exceptions.OperationalError``, which
    # ``enqueue_ingestion`` catches (best-effort). The worker's own consumer
    # reconnect is unaffected.
    broker_connection_retry_on_startup=False,
    broker_connection_max_retries=0,
    broker_transport_options={
        "max_retries": 0,
        "interval_start": 0,
        "interval_step": 0,
        "interval_max": 0,
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
    },
    broker_pool_limit=0,
    task_publish_retry=False,
)


@celery_app.task(name="lumen.ping")  # type: ignore[misc]  # celery's task decorator is untyped
def ping() -> str:
    """Trivial liveness task — returns ``"pong"``. Proves the worker round-trips."""
    return "pong"
