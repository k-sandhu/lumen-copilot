"""PR605 R6: repair must preserve a Processing attempt that becomes Ready.

Real ingestion, repositories, transactions and Ready CAS run on SQLite and,
with RUN_PR605_POSTGRES=1, migrated least-privilege lumentest_pr605. Only the
external adapters are controlled. Events and awaited task completion define
the interleaving; there are no sleeps, timing thresholds or retrieval retries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from uuid import UUID

from app.core.config import get_settings
from app.db import session as db_session
from app.db.repositories import ChunkRepository, DocumentRepository, SourceRepository
from app.domain.entities import DocumentStatus
from app.domain.llm import Embedding
from app.search import IndexedChunk
from app.tasks.index_sync import sync_document_index_async
from app.tasks.ingest import ingest_document_async
from tests.test_embedding_source_recovery import _seed
from tests.test_embedding_source_recovery import source_db as _source_db
from tests.test_index_sync import _FakeIndexStore, _FakeObjectStore

source_db = _source_db


async def test_processing_repair_preserves_same_attempt_ready_publication(source_db: bool) -> None:
    """R5-001: finish actual attempt N inside repair's post-snapshot network wait."""
    tenant_id, source_id = await _seed()
    settings = get_settings()
    async with db_session.tenant_session_scope(tenant_id) as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        document = await DocumentRepository(session, tenant_id).create(
            owner_id=source.owner_id,
            collection_id=UUID(source.config["collection_id"]),
            filename="race.txt",
            mime_type="text/plain",
            size_bytes=34,
            storage_key="race-key",
            acl_enforced=False,
        )
    objects = _FakeObjectStore()
    objects.put(str(tenant_id), document.storage_key, b"same generation repair race passage")
    embedding_started = asyncio.Event()
    allow_embedding = asyncio.Event()

    class _Gateway:
        async def embed(self, inputs: Sequence[str], **kwargs: object) -> list[Embedding]:
            embedding_started.set()
            await allow_embedding.wait()
            return [
                Embedding(vector=[0.125] * settings.llm_embedding_dimensions, model="native-fake")
                for _ in inputs
            ]

    class _Store(_FakeIndexStore):
        def __init__(self) -> None:
            super().__init__()
            self.repair_started = False
            self.searchable: dict[tuple[UUID, int], IndexedChunk] = {}
            self.search_calls = 0

        async def ensure_index(self) -> None:
            await super().ensure_index()
            if self.repair_started:
                return  # ingestion's attempt-owned publication is not blocked
            self.repair_started = True
            async with db_session.tenant_session_scope(tenant_id) as session:
                processing = await DocumentRepository(session, tenant_id).get(document.id)
                chunks = await ChunkRepository(session, tenant_id).list_for_document(document.id)
            assert processing is not None
            assert processing.status is DocumentStatus.PROCESSING
            assert processing.ingestion_attempts == 1
            assert chunks == []
            # Repair has snapshotted Processing/N and released its transaction.
            # Model its ensure_index network wait by completing ingestion now.
            allow_embedding.set()
            outcome = await ingestion
            assert outcome.status is DocumentStatus.READY
            assert outcome.chunk_count > 0
            assert self.searchable

        async def upsert_chunks(
            self, chunks: Sequence[IndexedChunk], *, refresh: bool | str = False
        ) -> None:
            assert refresh == "wait_for"
            await super().upsert_chunks(chunks, refresh=True)
            self.searchable.update(
                ((chunk.chunk_id, chunk.ingestion_attempt), chunk) for chunk in chunks
            )

        async def delete_document_generation(
            self,
            *,
            tenant_id: UUID,
            document_id: UUID,
            ingestion_attempt: int,
            refresh: bool = False,
        ) -> None:
            await super().delete_document_generation(
                tenant_id=tenant_id,
                document_id=document_id,
                ingestion_attempt=ingestion_attempt,
                refresh=refresh,
            )
            self.searchable = {
                key: chunk
                for key, chunk in self.searchable.items()
                if chunk.ingestion_attempt != ingestion_attempt
            }

        async def delete_older_document_generations(
            self,
            *,
            tenant_id: UUID,
            document_id: UUID,
            ingestion_attempt: int,
            refresh: bool = False,
        ) -> None:
            await super().delete_older_document_generations(
                tenant_id=tenant_id,
                document_id=document_id,
                ingestion_attempt=ingestion_attempt,
                refresh=refresh,
            )
            self.searchable = {
                key: chunk
                for key, chunk in self.searchable.items()
                if chunk.ingestion_attempt >= ingestion_attempt
            }

        def search_once(self) -> list[IndexedChunk]:
            self.search_calls += 1
            return list(self.searchable.values())

    store = _Store()
    ingestion = asyncio.create_task(
        ingest_document_async(
            tenant_id,
            document.id,
            settings=settings,
            object_store=objects,  # type: ignore[arg-type]
            gateway=_Gateway(),  # type: ignore[arg-type]
            search_store=store,  # type: ignore[arg-type]
        )
    )
    try:
        await embedding_started.wait()
        repair = await sync_document_index_async(
            tenant_id,
            document.id,
            settings=settings,
            store=store,  # type: ignore[arg-type]
        )
    finally:
        allow_embedding.set()
        outcome = await ingestion

    async with db_session.tenant_session_scope(tenant_id) as session:
        ready = await DocumentRepository(session, tenant_id).get(document.id)
        chunks = await ChunkRepository(session, tenant_id).list_for_document(document.id)
    assert ready is not None
    assert ready.status is DocumentStatus.READY
    assert ready.ingestion_attempts == 1
    assert outcome.chunk_count == len(chunks) > 0
    hits = store.search_once()
    assert {hit.chunk_id for hit in hits} == {
        chunk.id for chunk in chunks
    }, "repair erased the same generation just published Ready"
    assert all(hit.ingestion_attempt == 1 for hit in hits)
    assert all(hit.embedding_fingerprint == settings.embedding_space_fingerprint for hit in hits)
    assert all(len(hit.embedding) == settings.llm_embedding_dimensions for hit in hits)
    assert store.search_calls == 1
    assert store.deleted_generations == []
    assert all(attempt == 1 for _, _, attempt in store.deleted_older_generations)
    assert repair.indexed_count == 0  # repair did not republish its empty snapshot
    assert len(store.upserts) == 1  # only ingestion published
