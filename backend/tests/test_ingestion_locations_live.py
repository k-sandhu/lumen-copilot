"""Provenance round-trip against an explicitly isolated ingestion test database."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.db.repositories import (
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.db.session import dispose_engine, session_scope, tenant_session_scope
from app.domain.entities import Role
from app.domain.llm import Embedding
from app.ingestion.fingerprint import build_ingestion_fingerprint
from app.ingestion.parsers import parse_document_with_locations
from app.tasks.ingest import ingest_document_async
from tests.test_ingestion_locations import _make_pdf_pages_with_blank_middle, _NoopIndexStore
from tests.test_ingestion_task import _FakeObjectStore


@pytest.mark.live
async def test_postgres_provenance_round_trip_is_tenant_scoped() -> None:
    settings = get_settings()
    address = urlsplit(settings.database_url)
    if (
        address.hostname not in ("localhost", "127.0.0.1")
        or address.port != 47182
        or address.path != "/lumentest_pr625"
    ):
        pytest.skip("requires the explicitly isolated ingestion test database")

    class Gateway:
        async def embed(
            self,
            inputs: Sequence[str],
            *,
            model: str | None = None,
            cache_namespace: str | None = None,
        ) -> list[Embedding]:
            return [
                Embedding(vector=[0.1] * settings.llm_embedding_dimensions, model="synthetic")
                for _ in inputs
            ]

    data = _make_pdf_pages_with_blank_middle()
    parsed = parse_document_with_locations(data, mime_type="application/pdf")
    # Only the global tenant bootstrap uses an unscoped session. All scoped
    # entities and the actual Celery pipeline use tenant_session_scope.
    async with session_scope() as session:
        tenant = await TenantRepository(session).create(name=f"ingestion-fixture-{uuid4()}")
    try:
        async with tenant_session_scope(tenant.id) as session:
            user = await UserRepository(session, tenant.id).create(
                email=f"fixture-{uuid4()}@example.test",
                password_hash="fixture",
                roles=[Role.MEMBER],
            )
            collection = await CollectionRepository(session, tenant.id).create(
                owner_id=user.id, name="Synthetic provenance"
            )
            document = await DocumentRepository(session, tenant.id).create(
                owner_id=user.id,
                collection_id=collection.id,
                filename="synthetic.pdf",
                mime_type="application/pdf",
                size_bytes=len(data),
                storage_key="synthetic.pdf",
                acl_enforced=False,
            )
        store = _FakeObjectStore()
        store.put(str(tenant.id), document.storage_key, data)
        await ingest_document_async(
            tenant.id,
            document.id,
            settings=settings,
            object_store=store,
            gateway=Gateway(),
            search_store=_NoopIndexStore(),
        )
        async with tenant_session_scope(tenant.id) as session:
            retained = await DocumentRepository(session, tenant.id).get(document.id)
            assert retained is not None
            assert retained.source_text == parsed.text
            assert retained.source_locations == parsed.locations
            chunks = await ChunkRepository(session, tenant.id).list_for_document(document.id)
            assert chunks
            expected = build_ingestion_fingerprint(
                data,
                mime_type="application/pdf",
                chunk_size=settings.ingestion_chunk_size,
                overlap=settings.ingestion_chunk_overlap,
                embeddings=[Embedding(vector=list(chunks[0].embedding), model="synthetic")],
            )
            assert (retained.ingestion_metadata or {}).get("ingestion_fingerprint") == expected
            for chunk in chunks:
                assert parsed.text[chunk.char_start : chunk.char_end] == chunk.text
                assert chunk.source_locations == parsed.locations_for_span(
                    chunk.char_start, chunk.char_end
                )
        other_tenant_id = uuid4()
        async with tenant_session_scope(other_tenant_id) as session:
            assert await DocumentRepository(session, other_tenant_id).get(document.id) is None
            assert await ChunkRepository(session, other_tenant_id).get(chunks[0].id) is None
    finally:
        await dispose_engine()
    # The caller tears down the entire fresh test database after validation.
