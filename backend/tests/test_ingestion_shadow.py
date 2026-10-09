from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_shadow_failure_and_diagnostic_failure_preserve_baseline():
    from app.domain.ingestion_shadow import CandidateExtraction
    from app.services.ingestion_shadow import extract_with_mode

    async def baseline():
        return "Exact é🙂 evidence"

    async def failed():
        raise RuntimeError("untrusted contents")

    async def broken_sink(value):
        raise RuntimeError("datastore unavailable")

    for candidate in (failed,):
        result = await extract_with_mode(
            mode="shadow",
            source_format="pdf",
            baseline=baseline,
            candidate=candidate,
            record=broken_sink,
        )
        assert result.text == "Exact é🙂 evidence"
        assert result.comparison.status == "failed"

    async def partial():
        return CandidateExtraction("short", "partial", 1)

    result = await extract_with_mode(
        mode="shadow", source_format="pdf", baseline=baseline, candidate=partial, record=broken_sink
    )
    assert result.text == "Exact é🙂 evidence" and result.comparison.status == "partial"
    with pytest.raises(ValueError, match="complete"):
        await extract_with_mode(
            mode="native",
            source_format="pdf",
            baseline=baseline,
            candidate=partial,
            record=broken_sink,
        )


def test_modes_are_independent_and_unknown_modes_reject():
    from pydantic import ValidationError

    from app.core.config import Settings

    settings = Settings(ingestion_format_modes={"pdf": "shadow", "docx": "native"})
    assert settings.ingestion_format_modes["pdf"] == "shadow"
    assert settings.ingestion_format_modes["docx"] == "native"
    assert settings.ingestion_format_modes.get("xlsx", "python") == "python"
    with pytest.raises(ValidationError):
        Settings(ingestion_format_modes={"pdf": "surprise"})
    with pytest.raises(ValidationError):
        Settings(ingestion_format_modes={"archive": "native"})


from tests.test_ingestion_task import sqlite_engine  # noqa: E402, F401


@pytest.mark.asyncio
async def test_shadow_records_are_scoped_content_free_and_idempotent(sqlite_engine):  # noqa: F811
    from dataclasses import asdict

    import app.db.session as db_session
    from app.db.ingestion_shadow import ShadowRepository
    from app.domain.ingestion_shadow import ShadowComparison
    from tests.test_ingestion_task import _seed_document

    tenant, doc = await _seed_document(mime_type="text/plain", key="fixture")
    other, _ = await _seed_document(mime_type="text/plain", key="other")
    value = ShadowComparison("text", "indexed", 8, 8, True, 0, 1)
    async with db_session.session_scope() as session:
        repo = ShadowRepository(session, tenant)
        await repo.record(doc, "a" * 64, value)
        await repo.record(doc, "a" * 64, value)
        rows = await repo.page(limit=10)
        assert len(rows) == 1 and rows[0].document_id == doc
        assert asdict(rows[0].comparison) == asdict(value)
        assert not await ShadowRepository(session, other).page(limit=10)
        with pytest.raises(ValueError, match="document"):
            await ShadowRepository(session, other).record(doc, "a" * 64, value)


@pytest.mark.asyncio
async def test_original_replay_keeps_citations_and_enforces_admin_permissions(
    sqlite_engine,  # noqa: F811
    monkeypatch,  # noqa: F811
):  # noqa: F811
    from uuid import uuid4

    import app.db.session as db_session
    import app.services.ingestion_admin as admin_module
    from app.auth.principal import Principal
    from app.core.config import Settings
    from app.core.errors import ForbiddenError, NotFoundError
    from app.db.repositories import (
        ChatSessionRepository,
        ChunkInput,
        ChunkRepository,
        CitationRepository,
        DocumentRepository,
        MessageRepository,
    )
    from app.domain.entities import MessageRole, Role
    from app.domain.ingestion_shadow import CandidateExtraction
    from app.services.ingestion_admin import IngestionAdminService
    from tests._audit_helpers import RecordingDurableAuditTransactions, denial_context
    from tests.test_ingestion_task import _FakeObjectStore, _seed_document

    tenant, doc = await _seed_document(mime_type="text/plain", key="fixture")
    foreign, foreign_doc = await _seed_document(mime_type="text/plain", key="foreign")
    store = _FakeObjectStore()
    store.put(str(tenant), "fixture", b"Original evidence")
    ledger = RecordingDurableAuditTransactions()
    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant).get(doc)
        owner = document.owner_id
        chunks = await ChunkRepository(session, tenant).replace_for_document(
            doc, [ChunkInput(text="Old evidence", char_start=0, char_end=12)]
        )
        chat = await ChatSessionRepository(session, tenant).create(owner_id=owner, model="fixture")
        message = await MessageRepository(session, tenant).add(
            session_id=chat.id, role=MessageRole.ASSISTANT, content="old answer"
        )
        cite = await CitationRepository(session, tenant).add(
            message_id=message.id, chunk_id=chunks[0].id, char_start=0, char_end=12
        )

    async def candidate(*args, **kwargs):
        return CandidateExtraction("Candidate evidence", "indexed", 1)

    monkeypatch.setattr(admin_module, "extract_format_candidate", candidate)
    principal = Principal(owner, tenant, (Role.ADMIN,))
    service = IngestionAdminService(
        principal,
        settings=Settings(),
        object_store=store,
        denials=denial_context(ledger, object(), tenant, owner),
    )
    replay = await service.replay(doc)
    assert replay.baseline_chars == len("Original evidence")
    async with db_session.session_scope() as session:
        after = await ChunkRepository(session, tenant).list_for_document(doc)
        citations = await CitationRepository(session, tenant).list_for_message(message.id)
        assert after[0].id == chunks[0].id and after[0].text == "Old evidence"
        assert citations[0].id == cite.id and citations[0].chunk_id == chunks[0].id
        assert citations[0].char_start == 0 and citations[0].char_end == 12
    assert (await service.report(document_id=doc)).rows[0].document_id == doc
    assert (await service.preview(document_id=doc)).documents == ((doc, "text"),)
    assert (await service.preview(collection_id=document.collection_id)).documents == (
        (doc, "text"),
    )
    with pytest.raises(ForbiddenError, match="owner approval"):
        await service.execute_generation(document_id=doc)
    with pytest.raises(NotFoundError):
        await service.report(document_id=foreign_doc)
    denied = IngestionAdminService(
        Principal(owner, tenant, (Role.MEMBER,)),
        settings=Settings(),
        object_store=store,
        denials=denial_context(ledger, object(), tenant, owner),
    )
    with pytest.raises(ForbiddenError):
        await denied.report(document_id=doc)
    other = uuid4()
    unauthorized = IngestionAdminService(
        Principal(other, tenant, (Role.ADMIN,)),
        settings=Settings(),
        object_store=store,
        denials=denial_context(ledger, object(), tenant, other),
    )
    with pytest.raises(NotFoundError):
        await unauthorized.replay(doc)
    assert len([event for event in ledger.events if event.action == "permission.denied"]) == 4


@pytest.mark.asyncio
async def test_shadow_mode_in_pipeline_and_native_reingestion_guard(sqlite_engine, monkeypatch):  # noqa: F811
    import app.db.session as db_session
    import app.tasks.ingest as ingest
    from app.db.ingestion_shadow import ShadowRepository
    from app.db.repositories import ChunkRepository
    from app.domain.ingestion_shadow import CandidateExtraction
    from tests.test_ingestion_task import (
        _FakeGateway,
        _FakeIndexStore,
        _FakeObjectStore,
        _seed_document,
        _settings,
    )

    tenant, doc = await _seed_document(mime_type="text/plain", key="fixture")
    store = _FakeObjectStore()
    store.put(str(tenant), "fixture", b"Exact baseline")

    async def candidate(*args, **kwargs):
        return CandidateExtraction("different", "partial", 1)

    monkeypatch.setattr(ingest, "extract_format_candidate", candidate)
    await ingest.ingest_document_async(
        tenant,
        doc,
        settings=_settings(ingestion_format_modes={"text": "shadow"}),
        object_store=store,
        gateway=_FakeGateway(),
        search_store=_FakeIndexStore(),
    )
    async with db_session.session_scope() as session:
        chunks = await ChunkRepository(session, tenant).list_for_document(doc)
        records = await ShadowRepository(session, tenant).page(limit=10)
        assert chunks[0].text == "Exact baseline" and records[0].comparison.status == "partial"
    result = await ingest.ingest_document_async(
        tenant,
        doc,
        settings=_settings(ingestion_format_modes={"text": "native"}),
        object_store=store,
        gateway=_FakeGateway(),
        search_store=_FakeIndexStore(),
    )
    assert result.error == "immutable_generation_policy_required"
    async with db_session.session_scope() as session:
        assert (await ChunkRepository(session, tenant).list_for_document(doc))[0].id == chunks[0].id


def test_cli_scopes_pages_and_format_summary():
    from uuid import uuid4

    from app.domain.ingestion_shadow import ShadowComparison, ShadowRecord
    from app.ingestion.cutover import parse_args
    from app.services.ingestion_admin import summarize

    args = parse_args(["preview", "--token-file", "fixture-token", "--collection", str(uuid4())])
    assert args.pages == 1 and args.collection is not None
    with pytest.raises(SystemExit):
        parse_args(["preview", "--token-file", "fixture-token", "--pages", "0"])
    value = ShadowComparison("pdf", "partial", 10, 5, False, 9, 1)
    assert summarize([ShadowRecord(uuid4(), uuid4(), value)]) == {
        "pdf": {
            "records": 1,
            "partial": 1,
            "exact_equal": 0,
            "baseline_chars": 10,
            "candidate_chars": 5,
            "positional_mismatches": 9,
        }
    }
