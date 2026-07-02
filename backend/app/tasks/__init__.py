"""Background jobs — the only place Celery tasks are defined or enqueued.

Single responsibility (ADR-0004 boundary table): own the Celery app and every
task. **Nobody else may define or enqueue a Celery task.** All slow or burst-y
work (parsing, chunking, embedding, connector sync, re-index) runs here, never
in the request path; tasks are idempotent, retried with backoff, dead-lettered
(CC-5). Ships the app + the trivial ``ping`` task (so the ``worker`` compose
service boots), the document **ingestion** task (#21): the parse → chunk →
embed → persist pipeline, enqueued from ``DocumentService.upload`` via
:func:`enqueue_ingestion`; and the connector **sync** task (#20, ADR-0009 §5):
fetch a source via its connector → reuse the ingestion pipeline → advance the
source status, enqueued from ``SourcesService`` via :func:`enqueue_source_sync`;
and the search **index sync** task (ADR-0010 §5, epic #189): make the OpenSearch
index match Postgres for one document, enqueued after-commit from the deletion
paths via :func:`enqueue_index_sync` (ingestion calls its async core in-band).
Each enqueue helper is the single enqueue point for its task.
"""

from app.tasks.celery_app import celery_app
from app.tasks.index_sync import enqueue_index_sync, sync_document_index
from app.tasks.ingest import enqueue_ingestion, ingest_document
from app.tasks.sync_source import enqueue_source_sync, sync_source

__all__ = [
    "celery_app",
    "enqueue_index_sync",
    "enqueue_ingestion",
    "enqueue_source_sync",
    "ingest_document",
    "sync_document_index",
    "sync_source",
]
