"""Durable checkpoint reuse and fencing with SQLite and adapter faults."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

import app.db.session as db_session
import app.tasks.ingest as ingest_module
from app.db import models
from app.db.ingestion_stages import StageRepository
from app.db.repositories import ChunkRepository, DocumentRepository
from app.domain.ingestion_stages import STAGES, StageOutput, StageOwnershipLost
from app.services.ingestion_stages import CheckpointPipeline, checksum
from app.tasks.ingestion_stages import DurableStageStore
from tests import test_ingestion_task as fixtures

sqlite_engine = fixtures.sqlite_engine
_offline_index_store = fixtures._offline_index_store


async def test_embedding_fault_retry_reuses_extraction(sqlite_engine, monkeypatch) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "key", b"Faithful source with enough text for one passage.")
    settings = fixtures._settings()
    original = ingest_module.parse_document
    calls = 0

    def parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest_module, "parse_document", parse)
    with pytest.raises(ingest_module.IngestionError):
        await ingest_module.ingest_document_async(
            tenant,
            document,
            settings=settings,
            object_store=store,
            gateway=fixtures._FakeGateway(fail=True),
        )
    await ingest_module.ingest_document_async(
        tenant, document, settings=settings, object_store=store, gateway=fixtures._FakeGateway()
    )
    assert calls == 1
    async with db_session.tenant_session_scope(tenant) as session:
        stages = await StageRepository(session, tenant).list(document)
        assert [s.stage for s in stages] == [
            "detect",
            "extract",
            "ocr",
            "normalize",
            "classify",
            "chunk",
            "embed",
            "index",
        ]
        chunks = await ChunkRepository(session, tenant).list_for_document(document)
        assert len({c.ord for c in chunks}) == len(chunks)
        assert all("local_path" not in s.payload_json for s in stages)


async def test_checkpoint_writes_are_tenant_and_attempt_fenced(sqlite_engine) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as session:
        claimed = await DocumentRepository(session, tenant).begin_ingestion(document)
        assert claimed is not None
        attempt = claimed.ingestion_attempts
    payload = json.dumps({"text": "exact"})
    artifact = StageOutput("extract", "a" * 64, checksum(payload), payload)
    async with db_session.tenant_session_scope(uuid4()) as session:
        with pytest.raises(StageOwnershipLost):
            await StageRepository(session, uuid4()).save(document, artifact, attempt=attempt)
    async with db_session.tenant_session_scope(tenant) as session:
        with pytest.raises(StageOwnershipLost):
            await StageRepository(session, tenant).save(document, artifact, attempt=attempt + 1)
        assert await StageRepository(session, tenant).list(document) == []


async def test_deleted_document_cannot_leave_stage_outputs(sqlite_engine) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as session:
        claimed = await DocumentRepository(session, tenant).begin_ingestion(document)
        assert claimed is not None
        payload = json.dumps({"text": "exact"})
        await StageRepository(session, tenant).save(
            document,
            StageOutput("extract", "a" * 64, checksum(payload), payload),
            attempt=claimed.ingestion_attempts,
        )
    async with db_session.tenant_session_scope(tenant) as session:
        await DocumentRepository(session, tenant).delete(document)
    async with db_session.tenant_session_scope(tenant) as session:
        assert (await session.execute(select(models.IngestionStageOutput))).scalars().all() == []


async def test_cache_clear_removes_outputs_and_current_stage_atomically(sqlite_engine) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as session:
        claimed = await DocumentRepository(session, tenant).begin_ingestion(document)
        assert claimed is not None
        repo = StageRepository(session, tenant)
        payload = json.dumps({"text": "exact"})
        await repo.save(
            document,
            StageOutput("extract", "a" * 64, checksum(payload), payload),
            attempt=claimed.ingestion_attempts,
        )
    async with db_session.tenant_session_scope(tenant) as session:
        await StageRepository(session, tenant).clear(document)
    async with db_session.tenant_session_scope(tenant) as session:
        assert await StageRepository(session, tenant).list(document) == []
        row = await session.get(models.Document, document)
        assert row is not None and row.ingestion_stage is None


async def test_audit_failure_rolls_back_stage_output(sqlite_engine, monkeypatch) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    async with db_session.tenant_session_scope(tenant) as session:
        claimed = await DocumentRepository(session, tenant).begin_ingestion(document)
        assert claimed is not None
        attempt = claimed.ingestion_attempts

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.tasks.ingestion_stages.AuditSink.emit", fail_audit)
    payload = json.dumps({"text": "exact"})
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await DurableStageStore(tenant, document, attempt).save(
            StageOutput("extract", "a" * 64, checksum(payload), payload)
        )
    async with db_session.tenant_session_scope(tenant) as session:
        assert await StageRepository(session, tenant).list(document) == []
        row = await session.get(models.Document, document)
        assert row.ingestion_stage is None


async def test_index_fault_reuses_embeddings_and_republishes_current_attempt(
    sqlite_engine, monkeypatch
) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "key", b"Faithful source with enough text for one passage.")
    gateway = fixtures._FakeGateway()
    settings = fixtures._settings()
    original = ingest_module._sync_index
    attempts = []

    async def index(*args, **kwargs):
        attempts.append(kwargs["expected_attempt"])
        if len(attempts) == 1:
            raise ingest_module.IngestionError("index unavailable", code="ingestion_index_error")
        return await original(*args, **kwargs)

    monkeypatch.setattr(ingest_module, "_sync_index", index)
    with pytest.raises(ingest_module.IngestionError):
        await ingest_module.ingest_document_async(
            tenant, document, settings=settings, object_store=store, gateway=gateway
        )
    result = await ingest_module.ingest_document_async(
        tenant, document, settings=settings, object_store=store, gateway=gateway
    )
    assert result.status.value == "ready"
    assert len(gateway.calls) == 1
    assert attempts[1] > attempts[0]
    async with db_session.tenant_session_scope(tenant) as session:
        events = (await session.execute(select(models.AuditEvent))).scalars().all()
        stage_events = [e for e in events if e.action == "document.processing_stage_completed"]
        assert len(stage_events) == len(STAGES)
        assert all(
            set(e.event_metadata) == {"stage", "fingerprint", "output_sha256"} for e in stage_events
        )


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("boundary", ["before_compute", "after_compute", "after_commit"])
async def test_durable_task_fault_at_every_boundary_leaves_one_active_generation(
    sqlite_engine, monkeypatch, stage, boundary
) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "key", b"Exact evidence with enough text for one passage.")
    settings = fixtures._settings()
    gateway = fixtures._FakeGateway()
    tripped = False

    def fault(point, name):
        nonlocal tripped
        if point == boundary and name == stage and not tripped:
            tripped = True
            raise RuntimeError("injected stage crash")

    monkeypatch.setattr(
        ingest_module,
        "CheckpointPipeline",
        lambda *args, **kwargs: CheckpointPipeline(*args, **kwargs, fault=fault),
    )
    with pytest.raises(ingest_module.IngestionError):
        await ingest_module.ingest_document_async(
            tenant, document, settings=settings, object_store=store, gateway=gateway
        )
    result = await ingest_module.ingest_document_async(
        tenant, document, settings=settings, object_store=store, gateway=gateway
    )
    assert tripped and result.status.value == "ready"
    async with db_session.tenant_session_scope(tenant) as session:
        cached = await StageRepository(session, tenant).list(document)
        assert [o.stage for o in cached] == list(STAGES)
        chunks = await ChunkRepository(session, tenant).list_for_document(document)
        assert len(chunks) == result.chunk_count == len({c.ord for c in chunks})


async def test_settings_changes_reuse_extraction_but_changed_source_does_not(
    sqlite_engine, monkeypatch
) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "key", b"A faithful passage.")
    original = ingest_module.parse_document
    calls = 0

    def parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest_module, "parse_document", parse)
    gateway = fixtures._FakeGateway()
    for settings in [
        fixtures._settings(),
        fixtures._settings(INGESTION_CHUNK_SIZE=800),
        fixtures._settings(INGESTION_CHUNK_SIZE=800, LLM_EMBEDDING_MODEL="changed"),
    ]:
        await ingest_module.ingest_document_async(
            tenant, document, settings=settings, object_store=store, gateway=gateway
        )
    assert calls == 1
    assert len(gateway.calls) == 3
    store.put(str(tenant), "key", b"A revised passage.")
    await ingest_module.ingest_document_async(
        tenant, document, settings=settings, object_store=store, gateway=gateway
    )
    assert calls == 2


@pytest.mark.parametrize("fault", ["nonfinite", "blank_model", "wrong_dimension"])
async def test_invalid_embedding_cannot_create_a_cached_or_active_generation(
    sqlite_engine, monkeypatch, fault
) -> None:
    tenant, document = await fixtures._seed_document(mime_type="text/plain", key="key")
    store = fixtures._FakeObjectStore()
    store.put(str(tenant), "key", b"Exact evidence.")
    gateway = fixtures._FakeGateway()

    async def embed(inputs, **kwargs):
        from app.domain.llm import Embedding

        return [
            Embedding(
                vector=(
                    [float("nan")] * fixtures._DIM
                    if fault == "nonfinite"
                    else [1.0] * (fixtures._DIM - (fault == "wrong_dimension"))
                ),
                model="" if fault == "blank_model" else "fake",
            )
            for _ in inputs
        ]

    monkeypatch.setattr(gateway, "embed", embed)
    result = await ingest_module.ingest_document_async(
        tenant, document, settings=fixtures._settings(), object_store=store, gateway=gateway
    )
    assert result.status.value == "failed"
    async with db_session.tenant_session_scope(tenant) as session:
        assert await ChunkRepository(session, tenant).list_for_document(document) == []
        assert "embed" not in {
            o.stage for o in await StageRepository(session, tenant).list(document)
        }
