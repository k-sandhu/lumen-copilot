"""Administrator inspection never widens document access and commits its audit."""

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.repositories import AuditEventRepository
from tests import test_documents_api as fixtures

sessionmaker = fixtures.sessionmaker
seeded = fixtures.seeded
store = fixtures.store
app = fixtures.app
client = fixtures.client
enqueued = fixtures.enqueued

_DIAGNOSTICS = {
    "character_count": 9,
    "replacement_characters": 0,
    "suspicious_controls": 0,
    "source_part_kind": "page",
    "total_parts": 2,
    "parts_with_text": 1,
    "blank_parts": [2],
    "table_probe": "unavailable",
    "table_regions": None,
    "table_cells": None,
    "missing_table_cells": None,
    "warnings": ["blank_native_parts", "pdf_table_detection_unavailable"],
}


async def _document(factory, object_store, tenant_id: UUID, email: str) -> UUID:
    collection = await fixtures._seed_collection(factory, tenant_id=tenant_id, owner_email=email)
    document_id = await fixtures._seed_document(
        factory, object_store, tenant_id=tenant_id, owner_email=email, collection_id=collection
    )
    async with factory() as session:
        await session.execute(
            update(models.Document)
            .where(models.Document.id == document_id)
            .values(
                ingestion_metadata={"source_locations": [], "extraction_diagnostics": _DIAGNOSTICS}
            )
        )
        await session.commit()
    return document_id


async def _promote(factory, tenant_id: UUID, email: str) -> None:
    async with factory() as session:
        await session.execute(
            update(models.User)
            .where(models.User.tenant_id == tenant_id, models.User.email == email)
            .values(roles=["member", "admin"])
        )
        await session.commit()


async def test_admin_inspection_get_and_list_commit_view_audits(
    client: AsyncClient,
    seeded: fixtures._Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: fixtures.FakeObjectStore,
) -> None:
    document_id = await _document(sessionmaker, store, seeded.tenant_a, seeded.alice_email)
    await _promote(sessionmaker, seeded.tenant_a, seeded.alice_email)
    token = await fixtures._login(client, seeded.alice_email)
    response = await client.get(f"/api/v1/documents/{document_id}", headers=fixtures._auth(token))
    assert response.status_code == 200
    # The media contract retains nullable fields (including duration_ms) in
    # document responses; unknown diagnostic coverage remains explicit null.
    expected = _DIAGNOSTICS
    assert response.json().get("extraction_diagnostics") == expected
    listing = await client.get("/api/v1/documents", headers=fixtures._auth(token))
    assert listing.status_code == 200
    assert listing.json()["items"][0].get("extraction_diagnostics") == expected
    # Independent read-back after the requests proves commit, not merely flush.
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    views = [
        e for e in events if e.action == "document.viewed" and e.resource_id == str(document_id)
    ]
    assert len(views) == 2
    assert all(e.metadata == {"form": "extraction_diagnostics"} for e in views)


async def test_member_receives_no_extraction_diagnostics(
    client: AsyncClient,
    seeded: fixtures._Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: fixtures.FakeObjectStore,
) -> None:
    document_id = await _document(sessionmaker, store, seeded.tenant_a, seeded.alice_email)
    token = await fixtures._login(client, seeded.alice_email)
    response = await client.get(f"/api/v1/documents/{document_id}", headers=fixtures._auth(token))
    assert response.status_code == 200
    assert response.json().get("extraction_diagnostics") is None
    listing = await client.get("/api/v1/documents", headers=fixtures._auth(token))
    assert listing.json()["items"][0].get("extraction_diagnostics") is None


@pytest.mark.parametrize("foreign_tenant", [False, True])
async def test_admin_role_does_not_override_tenant_or_document_visibility(
    client: AsyncClient,
    seeded: fixtures._Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: fixtures.FakeObjectStore,
    foreign_tenant: bool,
) -> None:
    tenant_id = seeded.tenant_b if foreign_tenant else seeded.tenant_a
    owner = seeded.carol_email if foreign_tenant else seeded.bob_email
    document_id = await _document(sessionmaker, store, tenant_id, owner)
    await _promote(sessionmaker, seeded.tenant_a, seeded.alice_email)
    token = await fixtures._login(client, seeded.alice_email)
    response = await client.get(f"/api/v1/documents/{document_id}", headers=fixtures._auth(token))
    assert response.status_code == 404
    assert "extraction_diagnostics" not in response.text
