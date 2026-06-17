"""Background jobs — the only place Celery tasks are defined or enqueued.

Single responsibility (ADR-0004 boundary table): own the Celery app and every
task. **Nobody else may define or enqueue a Celery task.** All slow or burst-y
work (parsing, chunking, embedding, connector sync, re-index) runs here, never
in the request path; tasks are idempotent, retried with backoff, dead-lettered
(CC-5). This skeleton ships only the app + a trivial ``ping`` task so the
``worker`` compose service boots.
"""

from app.tasks.celery_app import celery_app

__all__ = ["celery_app"]
