"""Regression coverage for native extraction outcomes (spec 0016 / issue #624)."""

from __future__ import annotations

import asyncio
import io
import uuid
from importlib import import_module

import pytest

from app.domain.entities import DocumentStatus
from app.domain.ingestion import ExtractionOutcome
from tests.test_ingestion_locations import _make_pdf_pages_with_blank_middle
from tests.test_ingestion_task import (
    _FakeGateway,
    _FakeIndexStore,
    _FakeObjectStore,
    _seed_document,
    _settings,
)

pytest_plugins = ("tests.test_ingestion_task",)

db_session = import_module("app.db.session")
repositories = import_module("app.db.repositories")
ingest = import_module("app.tasks.ingest")


def _blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


async def _run_ingestion(
    *, mime_type: str, body: bytes, key: str
) -> tuple[object, object, object, _FakeGateway]:
    tenant_id, document_id = await _seed_document(mime_type=mime_type, key=key)
    store = _FakeObjectStore()
    gateway = _FakeGateway()
    async with db_session.session_scope() as session:
        document = await repositories.DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        store.put(str(tenant_id), document.storage_key, body)

    result = await ingest.ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=store,
        gateway=gateway,
    )
    async with db_session.session_scope() as session:
        document = await repositories.DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        chunks = await repositories.ChunkRepository(session, tenant_id).list_for_document(
            document_id
        )
    return tenant_id, document, (result, chunks), gateway


@pytest.mark.asyncio
async def test_blank_pdf_persists_empty_failure_without_embedding(
    sqlite_engine: None,
) -> None:
    tenant_id, document, details, gateway = await _run_ingestion(
        mime_type="application/pdf", body=_blank_pdf(), key="blank-pdf"
    )
    result, chunks = details

    assert result.status.value == "failed"
    assert result.chunk_count == 0
    assert document.status.value == "failed"
    assert document.ingestion_outcome == "empty"
    assert document.ingestion_metadata["ingestion_outcome"] == "empty"
    assert chunks == []
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_text_ingestion_persists_indexed_outcome_and_api_projection(
    sqlite_engine: None,
) -> None:
    tenant_id, document, details, _gateway = await _run_ingestion(
        mime_type="text/plain",
        body=("Grounded text has a searchable passage. " * 10).encode(),
        key="indexed-text",
    )
    _result, chunks = details

    assert document.status.value == "ready"
    assert document.ingestion_outcome == "indexed"
    assert document.ingestion_metadata["ingestion_outcome"] == "indexed"
    assert chunks

    document_api = import_module("app.api.v1.documents")
    document_service = import_module("app.services.document_service")
    response = document_api._to_response(document_service.DocumentView(document, len(chunks)))
    assert response.ingestion_outcome == "indexed"
    assert response.searchable is True


@pytest.mark.asyncio
async def test_pdf_with_blank_native_page_is_partial(
    sqlite_engine: None,
) -> None:
    _tenant_id, document, details, _gateway = await _run_ingestion(
        mime_type="application/pdf",
        body=_make_pdf_pages_with_blank_middle(),
        key="partial-pdf",
    )
    result, chunks = details

    assert result.status.value == "ready"
    assert chunks
    assert document.ingestion_outcome == "partial"
    assert document.ingestion_metadata["ingestion_outcome"] == "partial"


@pytest.mark.asyncio
async def test_corrupt_pdf_persists_failed_outcome(
    sqlite_engine: None,
) -> None:
    _tenant_id, document, details, _gateway = await _run_ingestion(
        mime_type="application/pdf", body=b"%PDF-1.4 broken", key="corrupt-pdf"
    )
    result, chunks = details

    assert result.status.value == "failed"
    assert document.status.value == "failed"
    assert document.ingestion_outcome == "failed"
    assert document.ingestion_metadata["ingestion_outcome"] == "failed"
    assert chunks == []


@pytest.mark.asyncio
async def test_unsupported_mime_persists_unsupported_outcome(
    sqlite_engine: None,
) -> None:
    _tenant_id, document, details, _gateway = await _run_ingestion(
        mime_type="image/png", body=b"not a supported native document", key="unsupported"
    )
    result, chunks = details

    assert result.status.value == "failed"
    assert document.status.value == "failed"
    assert document.ingestion_outcome == "unsupported"
    assert document.ingestion_metadata["ingestion_outcome"] == "unsupported"
    assert chunks == []


@pytest.mark.asyncio
async def test_reingesting_empty_text_clears_previously_indexed_chunks(
    sqlite_engine: None,
) -> None:
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="rerun-empty")
    store = _FakeObjectStore()
    gateway = _FakeGateway()
    async with db_session.session_scope() as session:
        document = await repositories.DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        storage_key = document.storage_key
    store.put(str(tenant_id), storage_key, ("Previously indexed text. " * 20).encode())
    first = await ingest.ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=store,
        gateway=gateway,
    )
    assert first.chunk_count > 0

    store.put(str(tenant_id), storage_key, b" \n\t ")
    second = await ingest.ingest_document_async(
        tenant_id,
        document_id,
        settings=_settings(),
        object_store=store,
        gateway=gateway,
    )

    assert second.status.value == "failed"
    assert second.chunk_count == 0
    async with db_session.session_scope() as session:
        chunks = await repositories.ChunkRepository(session, tenant_id).list_for_document(
            document_id
        )
        document = await repositories.DocumentRepository(session, tenant_id).get(document_id)
    assert chunks == []
    assert document is not None
    assert document.ingestion_outcome == "empty"
    document_api = import_module("app.api.v1.documents")
    document_service = import_module("app.services.document_service")
    response = document_api._to_response(document_service.DocumentView(document, 0))
    assert response.ingestion_outcome == "empty"
    assert response.searchable is False


@pytest.mark.asyncio
async def test_cross_tenant_repository_read_does_not_disclose_ingestion_outcome(
    sqlite_engine: None,
) -> None:
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="tenant-scope")
    foreign_tenant_id = uuid.uuid4()

    async with db_session.session_scope() as session:
        assert (
            await repositories.DocumentRepository(session, foreign_tenant_id).get(document_id)
            is None
        )
        owned = await repositories.DocumentRepository(session, tenant_id).get(document_id)
    assert owned is not None
    assert owned.ingestion_outcome is None


@pytest.mark.parametrize("newer_outcome", ["empty", "partial"])
async def test_delayed_success_cannot_overwrite_newer_extraction_outcome(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch, newer_outcome: str
) -> None:
    """R1-001: pause A after real index sync, then fully publish connector update B."""
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="older")
    store = _FakeObjectStore()
    gateway = _FakeGateway()
    async with db_session.session_scope() as session:
        older = await repositories.DocumentRepository(session, tenant_id).get(document_id)
        assert older is not None
        store.put(str(tenant_id), older.storage_key, b"Older ordinary native text.")

    indexed = asyncio.Event()
    release = asyncio.Event()
    original_sync = ingest._sync_index

    async def delayed_sync(*args: object, **kwargs: object) -> bool:
        published = await original_sync(*args, **kwargs)
        if kwargs["expected_attempt"] == 1:
            assert published
            indexed.set()
            await release.wait()
        return published

    monkeypatch.setattr(ingest, "_sync_index", delayed_sync)
    first = asyncio.create_task(
        ingest.ingest_document_async(
            tenant_id,
            document_id,
            settings=_settings(),
            object_store=store,
            gateway=gateway,
            search_store=_FakeIndexStore(),
        )
    )
    indexed_wait = asyncio.create_task(indexed.wait())
    try:
        await asyncio.wait((indexed_wait, first), return_when=asyncio.FIRST_COMPLETED)
        if not indexed.is_set():
            await first
        assert indexed.is_set()
        body = _blank_pdf() if newer_outcome == "empty" else _make_pdf_pages_with_blank_middle()
        new_key = f"{tenant_id}/newer.pdf"
        store.put(str(tenant_id), new_key, body)
        async with db_session.session_scope() as session:
            updated = await repositories.DocumentRepository(session, tenant_id).update_from_sync(
                document_id,
                filename="newer.pdf",
                mime_type="application/pdf",
                size_bytes=len(body),
                storage_key=new_key,
                status=DocumentStatus.PENDING,
                acl_principals=None,
                acl_synced_at=None,
                acl_scope_ids=None,
            )
            assert updated is not None
        await ingest.ingest_document_async(
            tenant_id,
            document_id,
            settings=_settings(),
            object_store=store,
            gateway=gateway,
            search_store=_FakeIndexStore(),
        )
        async with db_session.session_scope() as session:
            before = await repositories.DocumentRepository(session, tenant_id).get(document_id)
            chunks_before = await repositories.ChunkRepository(
                session, tenant_id
            ).list_for_document(document_id)
        assert before is not None
        assert before.ingestion_outcome == newer_outcome
        assert before.ingestion_attempts == 2
        assert before.status is (
            DocumentStatus.FAILED if newer_outcome == "empty" else DocumentStatus.READY
        )
        release.set()
        await first
        async with db_session.session_scope() as session:
            after = await repositories.DocumentRepository(session, tenant_id).get(document_id)
            chunks_after = await repositories.ChunkRepository(session, tenant_id).list_for_document(
                document_id
            )
        assert after is not None
        assert after.ingestion_outcome == newer_outcome
        assert after == before
        assert chunks_after == chunks_before
    finally:
        release.set()
        indexed_wait.cancel()
        await asyncio.gather(indexed_wait, return_exceptions=True)
        await first


@pytest.mark.parametrize("terminal", ["ready", "failed"])
@pytest.mark.parametrize("claimant", ["stale", "foreign_tenant"])
async def test_rejected_terminal_publication_preserves_outcome_metadata(
    sqlite_engine: None, terminal: str, claimant: str
) -> None:
    """The outcome write shares both tenant and attempt admission with lifecycle."""
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="guarded")
    async with db_session.session_scope() as session:
        documents = repositories.DocumentRepository(session, tenant_id)
        older = await documents.begin_ingestion(document_id)
        assert older is not None
        before = await documents.begin_ingestion(document_id)
        assert before is not None
    async with db_session.session_scope() as session:
        documents = repositories.DocumentRepository(
            session, uuid.uuid4() if claimant == "foreign_tenant" else tenant_id
        )
        attempt = (
            before.ingestion_attempts if claimant == "foreign_tenant" else older.ingestion_attempts
        )
        if terminal == "ready":
            published = await documents.mark_ingestion_ready(
                document_id, expected_attempt=attempt, outcome=ExtractionOutcome.INDEXED
            )
        else:
            published = await documents.mark_ingestion_failed(
                document_id,
                expected_attempt=attempt,
                outcome=ExtractionOutcome.FAILED,
                code="test_failure",
                message="Rejected terminal publication",
            )
        assert published is None
    async with db_session.session_scope() as session:
        after = await repositories.DocumentRepository(session, tenant_id).get(document_id)
    assert after == before
