"""Readiness is activated after refreshed index synchronization (spec 0015)."""

from collections.abc import Sequence
from uuid import UUID

import pytest

from app.core.errors import DependencyError
from app.db.repositories import ChunkRepository, DocumentRepository
from app.db.session import tenant_session_scope
from app.domain.entities import DocumentStatus
from app.search import IndexedChunk
from app.tasks.ingest import IngestionError, ingest_document_async
from tests.test_index_sync import _FakeGateway, _FakeObjectStore, _seed, _settings
from tests.test_index_sync import sqlite_engine as sqlite_engine  # noqa: F401

pytestmark = pytest.mark.usefixtures("sqlite_engine")


class ObservedIndex:
    def __init__(self, *, fault: str | None = None) -> None:
        self.fault = fault
        self.states: list[DocumentStatus] = []
        self.refreshes: list[bool] = []
        self.visible: list[IndexedChunk] = []
        self.tenant_id: UUID | None = None
        self.document_id: UUID | None = None

    async def ensure_index(self) -> None:
        pass

    async def _observe(self, refresh: bool, operation: str) -> None:
        assert self.tenant_id is not None and self.document_id is not None
        async with tenant_session_scope(self.tenant_id) as session:
            document = await DocumentRepository(session, self.tenant_id).get(self.document_id)
            assert document is not None
            self.states.append(document.status)
        self.refreshes.append(refresh)
        if self.fault == operation:
            raise DependencyError("synthetic synchronization failure", code="search_unavailable")

    async def delete_document(
        self, *, tenant_id: UUID, document_id: UUID, refresh: bool = False
    ) -> None:
        self.tenant_id, self.document_id = tenant_id, document_id
        await self._observe(refresh, "delete")
        if refresh:
            self.visible = []

    async def upsert_chunks(self, chunks: Sequence[IndexedChunk], *, refresh: bool = False) -> None:
        await self._observe(refresh, "upsert")
        if refresh:
            self.visible = list(chunks)


async def test_ready_follows_visible_refreshed_index_writes() -> None:
    tenant_id, _, _, document_id = await _seed(chunk_texts=[])
    objects = _FakeObjectStore()
    objects.put(str(tenant_id), "k", b"Grounded native text.")
    index = ObservedIndex()
    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=objects,
        gateway=_FakeGateway(),
        search_store=index,
    )
    assert index.states == [DocumentStatus.PROCESSING, DocumentStatus.PROCESSING]
    assert index.refreshes == [True, True]
    assert index.visible and result.status is DocumentStatus.READY


@pytest.mark.parametrize("fault", ["delete", "upsert"])
async def test_failed_synchronization_never_activates_ready_and_retry_converges(fault: str) -> None:
    tenant_id, _, _, document_id = await _seed(chunk_texts=[])
    objects = _FakeObjectStore()
    objects.put(str(tenant_id), "k", b"Grounded native text.")
    index = ObservedIndex(fault=fault)
    with pytest.raises(IngestionError, match="could not index"):
        await ingest_document_async(
            tenant_id,
            document_id,
            settings=_settings(),
            object_store=objects,
            gateway=_FakeGateway(),
            search_store=index,
        )
    async with tenant_session_scope(tenant_id) as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None and document.status is DocumentStatus.PROCESSING
    index.fault = None
    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=objects,
        gateway=_FakeGateway(),
        search_store=index,
    )
    assert result.status is DocumentStatus.READY and index.visible


async def test_empty_reingestion_clears_index_and_never_reports_ready() -> None:
    tenant_id, _, _, document_id = await _seed(chunk_texts=["Previously indexed text"])
    objects = _FakeObjectStore()
    objects.put(str(tenant_id), "k", b" \n ")
    index = ObservedIndex()
    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=objects,
        gateway=_FakeGateway(),
        search_store=index,
    )
    assert result.status is DocumentStatus.FAILED and result.chunk_count == 0
    assert index.refreshes == [True]
    assert DocumentStatus.READY not in index.states
    async with tenant_session_scope(tenant_id) as session:
        assert await ChunkRepository(session, tenant_id).list_for_document(document_id) == []
