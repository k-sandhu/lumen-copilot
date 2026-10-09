"""Deterministic extraction fingerprints and their ingestion persistence."""

from __future__ import annotations

import hashlib
import importlib
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.domain.llm import Embedding
from tests import test_ingestion_task as task_fixtures
from tests.test_ingestion_task import (
    ChunkRepository,
    DocumentRepository,
    IngestionError,
    _FakeGateway,
    _FakeObjectStore,
    _seed_document,
    _settings,
    db_session,
    ingest_document_async,
)

sqlite_engine = task_fixtures.sqlite_engine
_offline_index_store = task_fixtures._offline_index_store


def _build(
    data: bytes = b"the source",
    *,
    mime_type: str = "text/plain",
    chunk_size: int = 120,
    overlap: int = 20,
    embeddings: Sequence[Embedding] = (),
) -> dict[str, object]:
    fingerprint_module = importlib.import_module("app.ingestion.fingerprint")
    return fingerprint_module.build_ingestion_fingerprint(
        data,
        mime_type=mime_type,
        chunk_size=chunk_size,
        overlap=overlap,
        embeddings=embeddings,
    )


def test_fingerprint_is_deterministic_and_records_source_and_chunker_settings() -> None:
    first = _build(embeddings=[Embedding(vector=[0.1, 0.2], model="returned-model")])
    second = _build(embeddings=[Embedding(vector=[0.1, 0.2], model="returned-model")])

    assert first == second
    assert first["schema_version"] == 1
    assert first["source_sha256"] == hashlib.sha256(b"the source").hexdigest()
    assert first["mime_type"] == "text/plain"
    assert first["parser_version"] == "native-1"
    assert re.fullmatch(r"[0-9a-f]{64}", str(first["parser_code_sha256"]))
    assert re.fullmatch(r"[0-9a-f]{64}", str(first["chunker_code_sha256"]))
    ingestion_dir = Path(__file__).parents[1] / "app" / "ingestion"
    for field, filename in (
        ("parser_code_sha256", "parsers.py"),
        ("chunker_code_sha256", "chunking.py"),
    ):
        source = (ingestion_dir / filename).read_bytes().replace(b"\r\n", b"\n")
        assert first[field] == hashlib.sha256(source).hexdigest()
    assert isinstance(first["parser_dependencies"], dict)
    assert first["chunker_version"] == "boundary-1"
    assert first["chunk_size"] == 120
    assert first["chunk_overlap"] == 20
    assert first["embedding_model"] == "returned-model"
    assert first["embedding_dimension"] == 2


def test_source_mime_and_chunk_settings_change_fingerprint() -> None:
    baseline = _build()
    assert _build(b"different source") != baseline
    assert _build(mime_type="text/markdown") != baseline
    assert _build(chunk_size=121) != baseline
    assert _build(overlap=19) != baseline


def test_fingerprint_uses_returned_embedding_model_and_dimension() -> None:
    actual = _build(embeddings=[Embedding(vector=[1.0, 2.0, 3.0], model="actual-model")])
    assert actual["embedding_model"] == "actual-model"
    assert actual["embedding_dimension"] == 3


@pytest.mark.parametrize(
    "embeddings",
    [
        [Embedding(vector=[1.0], model="")],
        [Embedding(vector=[], model="a")],
        [Embedding(vector=[1.0], model="a"), Embedding(vector=[2.0], model="b")],
        [Embedding(vector=[1.0], model="a"), Embedding(vector=[2.0, 3.0], model="a")],
        [Embedding(vector=[float("nan")], model="a")],
        [Embedding(vector=[float("inf")], model="a")],
    ],
    ids=["blank-model", "empty-vector", "mixed-models", "mixed-dimensions", "nan", "infinity"],
)
def test_invalid_embeddings_raise_typed_fingerprint_error(
    embeddings: Sequence[Embedding],
) -> None:
    fingerprint_module = importlib.import_module("app.ingestion.fingerprint")
    with pytest.raises(fingerprint_module.FingerprintError):
        _build(embeddings=embeddings)


def test_empty_embeddings_have_no_model_or_dimension() -> None:
    fingerprint = _build(embeddings=[])
    assert fingerprint["embedding_model"] is None
    assert fingerprint["embedding_dimension"] is None


def test_unsupported_mime_raises_typed_fingerprint_error() -> None:
    fingerprint_module = importlib.import_module("app.ingestion.fingerprint")
    with pytest.raises(fingerprint_module.FingerprintError):
        _build(mime_type="application/x-unknown")


async def test_ingestion_persists_fingerprint_matching_retained_source_and_chunks(
    sqlite_engine: None,
) -> None:
    del sqlite_engine
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="fingerprint")
    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        key = document.storage_key
    source = ("fingerprint source has several stable sentences. " * 8).encode()
    store.put(str(tenant_id), key, source)

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )

    assert result.chunk_count > 0
    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
    assert document is not None
    assert document.source_text == source.decode()
    fingerprint = (document.ingestion_metadata or {}).get("ingestion_fingerprint")
    assert isinstance(fingerprint, dict)
    assert fingerprint["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert fingerprint["chunk_size"] == settings.ingestion_chunk_size
    assert fingerprint["chunk_overlap"] == settings.ingestion_chunk_overlap
    assert fingerprint["embedding_model"] == "fake"
    assert fingerprint["embedding_dimension"] == 8
    assert len(chunks) == result.chunk_count
    assert all(source.decode()[chunk.char_start : chunk.char_end] == chunk.text for chunk in chunks)


async def test_failed_reingestion_preserves_previous_fingerprint_source_and_chunk_slices(
    sqlite_engine: None,
) -> None:
    del sqlite_engine
    settings = _settings()
    store = _FakeObjectStore()
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="atomic-fingerprint")
    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        key = document.storage_key
    old_source = ("old retained source with stable chunks. " * 8).encode()
    new_source = b"replacement data must not replace the retained generation"
    store.put(str(tenant_id), key, old_source)
    await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
    )
    async with db_session.session_scope() as session:
        before = await DocumentRepository(session, tenant_id).get(document_id)
        previous_chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
    assert before is not None
    previous_fingerprint = before.ingestion_metadata["ingestion_fingerprint"]
    previous_slices = [
        (chunk.text, chunk.char_start, chunk.char_end, chunk.ord) for chunk in previous_chunks
    ]

    store.put(str(tenant_id), key, new_source)
    with pytest.raises(IngestionError) as failure:
        await ingest_document_async(
            tenant_id,
            document_id,
            settings=settings,
            object_store=store,  # type: ignore[arg-type]
            gateway=_FakeGateway(fail=True),  # type: ignore[arg-type]
        )
    assert failure.value.code == "ingestion_embedding_error"

    async with db_session.session_scope() as session:
        after = await DocumentRepository(session, tenant_id).get(document_id)
        retained_chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
    assert after is not None
    assert after.source_text == old_source.decode()
    assert after.ingestion_metadata["ingestion_fingerprint"] == previous_fingerprint
    assert [
        (chunk.text, chunk.char_start, chunk.char_end, chunk.ord) for chunk in retained_chunks
    ] == previous_slices


@pytest.mark.parametrize("invalid_identity", ["mixed-models", "mixed-dimensions"])
async def test_invalid_embedding_identity_preserves_retained_generation(
    sqlite_engine: None,
    invalid_identity: str,
) -> None:
    del sqlite_engine
    settings = _settings()
    store = _FakeObjectStore()
    tenant_id, document_id = await _seed_document(
        mime_type="text/plain", key=f"invalid-fingerprint-{invalid_identity}"
    )
    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        key = document.storage_key
    old_source = ("previous generation remains citation safe. " * 10).encode()
    replacement_source = ("candidate generation has new content. " * 18).encode()
    store.put(str(tenant_id), key, old_source)
    await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
    )
    async with db_session.session_scope() as session:
        before = await DocumentRepository(session, tenant_id).get(document_id)
        old_chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
    assert before is not None
    old_fingerprint = before.ingestion_metadata["ingestion_fingerprint"]
    old_slices = [(chunk.text, chunk.char_start, chunk.char_end, chunk.ord) for chunk in old_chunks]

    class _InvalidIdentityGateway(_FakeGateway):
        async def embed(
            self,
            inputs: Sequence[str],
            *,
            model: str | None = None,
            cache_namespace: str | None = None,
        ) -> list[Embedding]:
            del model, cache_namespace
            return [
                Embedding(
                    vector=[0.25] * (8 if index == 0 or invalid_identity == "mixed-models" else 7),
                    model=(
                        "observed-a"
                        if index == 0 or invalid_identity == "mixed-dimensions"
                        else "observed-b"
                    ),
                )
                for index, _ in enumerate(inputs)
            ]

    store.put(str(tenant_id), key, replacement_source)
    with pytest.raises(IngestionError, match="could not fingerprint extraction"):
        await ingest_document_async(
            tenant_id,
            document_id,
            settings=settings,
            object_store=store,  # type: ignore[arg-type]
            gateway=_InvalidIdentityGateway(),  # type: ignore[arg-type]
        )

    async with db_session.session_scope() as session:
        after = await DocumentRepository(session, tenant_id).get(document_id)
        retained_chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
    assert after is not None
    assert after.source_text == old_source.decode()
    assert after.ingestion_metadata["ingestion_fingerprint"] == old_fingerprint
    assert [
        (chunk.text, chunk.char_start, chunk.char_end, chunk.ord) for chunk in retained_chunks
    ] == old_slices


async def test_empty_source_persists_fingerprint_without_embedding_identity(
    sqlite_engine: None,
) -> None:
    del sqlite_engine
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="empty-fingerprint")
    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        assert document is not None
        key = document.storage_key
    source = b"  \n\t "
    store.put(str(tenant_id), key, source)

    await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )

    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
    assert document is not None
    fingerprint = document.ingestion_metadata["ingestion_fingerprint"]
    assert fingerprint["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert fingerprint["embedding_model"] is None
    assert fingerprint["embedding_dimension"] is None
    assert gateway.calls == []


async def test_cross_tenant_missing_document_does_not_write_fingerprint(
    sqlite_engine: None,
) -> None:
    del sqlite_engine
    settings = _settings()
    store = _FakeObjectStore()
    first_tenant, _ = await _seed_document(mime_type="text/plain", key="tenant-a")
    second_tenant, second_document = await _seed_document(mime_type="text/plain", key="tenant-b")
    async with db_session.session_scope() as session:
        before = await DocumentRepository(session, second_tenant).get(second_document)
        assert before is not None
        original_metadata = before.ingestion_metadata

    result = await ingest_document_async(
        first_tenant,
        second_document,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
    )

    assert result.status.value == "failed"
    async with db_session.session_scope() as session:
        after = await DocumentRepository(session, second_tenant).get(second_document)
    assert after is not None
    assert after.ingestion_metadata == original_metadata
    assert "ingestion_fingerprint" not in (after.ingestion_metadata or {})
