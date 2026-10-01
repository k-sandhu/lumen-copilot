"""The optional fingerprint projection follows existing document permissions."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from tests import test_documents_api as api_fixtures
from tests.test_documents_api import (
    FakeObjectStore,
    _Seeded,
)
from tests.test_documents_api import (
    _auth as _api_auth,
)
from tests.test_documents_api import (
    _login as _api_login,
)
from tests.test_documents_api import (
    _seed_collection as _api_seed_collection,
)
from tests.test_documents_api import (
    _seed_document as _api_seed_document,
)

sessionmaker = api_fixtures.sessionmaker
seeded = api_fixtures.seeded
store = api_fixtures.store
app = api_fixtures.app
client = api_fixtures.client
enqueued = api_fixtures.enqueued


@pytest.fixture
async def fingerprint_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    store: FakeObjectStore,
) -> AsyncIterator[tuple[str, str, dict[str, object]]]:
    """Seed one owned document with a fingerprint and one legacy document."""
    tenant_id = seeded.tenant_a
    collection_id = await _api_seed_collection(
        sessionmaker, tenant_id=tenant_id, owner_email=seeded.alice_email
    )
    fingerprint_doc_id = await _api_seed_document(
        sessionmaker,
        store,
        tenant_id=tenant_id,
        owner_email=seeded.alice_email,
        collection_id=collection_id,
        filename="fingerprinted.txt",
    )
    legacy_doc_id = await _api_seed_document(
        sessionmaker,
        store,
        tenant_id=tenant_id,
        owner_email=seeded.alice_email,
        collection_id=collection_id,
        filename="legacy.txt",
    )
    fingerprint = {
        "schema_version": 1,
        "source_sha256": "a" * 64,
        "mime_type": "text/plain",
        "parser_version": "native-1",
        "parser_code_sha256": "b" * 64,
        "parser_dependencies": {},
        "chunker_version": "boundary-1",
        "chunker_code_sha256": "c" * 64,
        "chunk_size": 120,
        "chunk_overlap": 20,
        "embedding_model": "observed-model",
        "embedding_dimension": 3,
    }
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(models.Document).where(models.Document.id == fingerprint_doc_id)
            )
        ).scalar_one()
        row.ingestion_metadata = {"ingestion_fingerprint": fingerprint}
        await session.commit()
    yield (str(fingerprint_doc_id), str(legacy_doc_id), fingerprint)


async def test_get_document_projects_persisted_fingerprint_only_for_owner(
    client: AsyncClient,
    seeded: _Seeded,
    fingerprint_document: tuple[str, str, dict[str, object]],
) -> None:
    fingerprint_doc_id, _, fingerprint = fingerprint_document
    token = await _api_login(client, seeded.alice_email)

    response = await client.get(f"/api/v1/documents/{fingerprint_doc_id}", headers=_api_auth(token))

    assert response.status_code == 200, response.text
    assert response.json()["ingestion_fingerprint"] == fingerprint


async def test_get_legacy_document_keeps_fingerprint_unknown(
    client: AsyncClient,
    seeded: _Seeded,
    fingerprint_document: tuple[str, str, dict[str, object]],
) -> None:
    _, legacy_doc_id, _ = fingerprint_document
    token = await _api_login(client, seeded.alice_email)

    response = await client.get(f"/api/v1/documents/{legacy_doc_id}", headers=_api_auth(token))

    assert response.status_code == 200, response.text
    assert response.json().get("ingestion_fingerprint") is None


async def test_get_foreign_tenant_fingerprint_remains_not_found(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    collection_id = await _api_seed_collection(
        sessionmaker, tenant_id=seeded.tenant_b, owner_email=seeded.carol_email
    )
    foreign_doc_id = await _api_seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_b,
        owner_email=seeded.carol_email,
        collection_id=collection_id,
        filename="foreign.txt",
    )
    token = await _api_login(client, seeded.alice_email)

    response = await client.get(f"/api/v1/documents/{foreign_doc_id}", headers=_api_auth(token))

    assert response.status_code == 404
