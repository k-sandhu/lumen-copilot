"""Background jobs — the only place Celery tasks are defined or enqueued.

Single responsibility (ADR-0004 boundary table): own the Celery app and every
task. **Nobody else may define or enqueue a Celery task.** All slow or burst-y
work (parsing, chunking, embedding, connector sync, re-index) runs here, never
in the request path; tasks are idempotent, retried with backoff, dead-lettered
(CC-5). Ships the app + the trivial ``ping`` task (so the ``worker`` compose
service boots) and the document **ingestion** task (#21): the parse → chunk →
embed → persist pipeline, enqueued from ``DocumentService.upload`` via
:func:`enqueue_ingestion` — the single enqueue point.
"""

from app.tasks.celery_app import celery_app
from app.tasks.ingest import enqueue_ingestion, ingest_document

__all__ = ["celery_app", "enqueue_ingestion", "ingest_document"]
