"""Celery application + a trivial task.

The ``worker`` compose service runs ``celery -A app.tasks.celery_app worker``,
so this module must expose a ready-to-boot ``celery_app``. Broker and result
backend come from settings (``CELERY_BROKER_URL`` / ``CELERY_RESULT_BACKEND``,
both Redis) — read via ``core.config`` only, never ``os.environ`` here.

The ``ping`` task is a smoke task proving the worker round-trips; real tasks
(idempotent, retried, dead-lettered) land under CC-5.
"""

from __future__ import annotations

import structlog
from celery import Celery, signals

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
    # the connector sync task (#20), the artifact-retention janitor (#208), the
    # headless agent-run task (#235), the schedule-fire dispatcher (#236), the
    # run-digest beat task (#238), and the sandbox code-run task (#230) regardless of
    # package ``__init__`` side effects. The ``beat`` process fires
    # ``lumen.fire_schedule`` + ``lumen.build_run_digests``; the worker consumes them.
    imports=(
        "app.tasks.ingest",
        "app.tasks.sync_source",
        "app.tasks.connector_poll",
        "app.tasks.artifact_retention",
        "app.tasks.run_assistant",
        "app.tasks.run_reaper",
        "app.tasks.scheduler",
        "app.tasks.run_digest",
        "app.tasks.run_sandbox",
        "app.tasks.summarize",
        "app.tasks.upload_janitor",
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
    beat_schedule={
        "expired-upload-sweep": {
            "task": "lumen.sweep_expired_uploads",
            "schedule": float(_settings.upload_janitor_interval_seconds),
        },
        "stale-run-reaper": {
            "task": "lumen.reap_stale_runs",
            "schedule": float(_settings.run_reaper_interval_seconds),
        },
    },
)


@celery_app.on_after_finalize.connect  # type: ignore[misc]  # Celery signals are untyped
def _configure_periodic_tasks(sender: object, **kwargs: object) -> None:
    # Beat finalizes the app before constructing RedBeat (which snapshots conf).
    # beat_init alone is too late for its static entries/prefix/leader lock.
    from app.tasks.scheduler import configure_beat

    configure_beat()


@signals.beat_init.connect  # type: ignore[misc]  # Celery signals are untyped
def _reconcile_beat_schedules(sender: object, **kwargs: object) -> None:
    if getattr(sender, "app", None) is not celery_app:
        return
    from app.tasks.scheduler import reconcile_from_postgres

    log = structlog.get_logger(__name__)
    try:
        count = reconcile_from_postgres()
    except Exception as exc:
        log.error("schedule.boot_reconcile_failed", error_type=type(exc).__name__)
        # Celery Signal.send swallows Exception. SystemExit must escape so Beat
        # never starts ticking after a failed required boot reconciliation.
        raise SystemExit(1) from exc
    log.info("schedule.boot_reconciled", schedules=count)


@signals.worker_process_init.connect  # type: ignore[misc]  # Celery signals are untyped
def _start_telemetry(**kwargs: object) -> None:
    """Start trace/metric export in each pool child (ADR-0027 §5).

    Per child, not in the parent: exporter threads do not survive ``fork``, and
    each child needs its own ``service.instance.id`` so Prometheus keeps their
    cumulative series apart. A no-op unless ``TELEMETRY_ENABLED``.
    """
    from app.observability import configure_telemetry

    configure_telemetry(get_settings(), process="worker")


@signals.worker_process_shutdown.connect  # type: ignore[misc]  # Celery signals are untyped
def _stop_telemetry(**kwargs: object) -> None:
    """Flush the child's last spans and metrics before it exits."""
    from app.observability import shutdown_telemetry

    shutdown_telemetry()


@celery_app.task(name="lumen.ping")  # type: ignore[misc]  # celery's task decorator is untyped
def ping() -> str:
    """Trivial liveness task — returns ``"pong"``. Proves the worker round-trips."""
    return "pong"
