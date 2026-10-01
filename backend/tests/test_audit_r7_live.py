"""R7 terminal denial races on actual PostgreSQL under the restricted R6 fixture."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.api.deps import get_db_session, get_durable_audit_transactions_dep, get_object_store
from app.auth import mint_access_token
from app.core.config import get_settings
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    CollectionRepository,
    SavedSearchRepository,
)
from app.db.tenant_context import bind_tenant
from app.main import create_app
from tests.test_audit_r6_live import _LIVE_URL, _Store, live  # noqa: F401

pytestmark = pytest.mark.skipif(_LIVE_URL is None, reason="Targeted PostgreSQL opt-in required")


@pytest.mark.parametrize(
    "method,path,body,action,resource_type",
    [
        ("POST", "/api/v2/document-uploads", {}, "document.upload", "collection"),
        ("GET", "/api/v2/document-uploads/{id}", None, "document_upload.read", "document_upload"),
        (
            "POST",
            "/api/v2/document-uploads/{id}/parts",
            {"part_numbers": [1]},
            "document_upload.sign_parts",
            "document_upload",
        ),
        (
            "POST",
            "/api/v2/document-uploads/{id}/complete",
            {"parts": [{"part_number": 1, "etag": "test"}]},
            "document_upload.complete",
            "document_upload",
        ),
        (
            "DELETE",
            "/api/v2/document-uploads/{id}",
            None,
            "document_upload.abort",
            "document_upload",
        ),
        (
            "POST",
            "/api/v2/documents/{id}/access-url",
            {"purpose": "preview"},
            "document.access_url",
            "document",
        ),
        ("GET", "/api/v2/documents/{id}/transcript", None, "document.transcript.read", "document"),
    ],
)
async def test_merged_v2_denial_is_durable_under_rls(
    live,  # noqa: F811 — imported fixture
    method,
    path,
    body,
    action,
    resource_type,  # noqa: F811 — imported fixture
):
    """New routes use independent evidence without committing unrelated work."""
    target = uuid.uuid4()
    principal = live.principal
    application = create_app()
    application.dependency_overrides[get_durable_audit_transactions_dep] = lambda: live.transactions
    application.dependency_overrides[get_object_store] = lambda: _Store()
    async with live.factory() as caller:
        await bind_tenant(caller, principal.tenant_id)
        pending = await CollectionRepository(caller, principal.tenant_id).create(
            owner_id=principal.user_id, name="must roll back"
        )

        async def caller_session():
            yield caller

        application.dependency_overrides[get_db_session] = caller_session
        if not body and method == "POST":
            body = {
                "collection_id": str(target),
                "filename": "private.txt",
                "mime_type": "text/plain",
                "size_bytes": 1,
            }
        token = mint_access_token(principal, get_settings()).token
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, client=("203.0.113.91", 40000)),
            base_url="http://test",
            headers={"authorization": f"Bearer {token}", "x-request-id": "r9-v2-denial"},
        ) as client:
            response = await client.request(method, path.format(id=target), json=body)
            assert response.status_code == 404, response.text
        await caller.rollback()
        async with live.factory() as read:
            await bind_tenant(read, principal.tenant_id)
            assert await CollectionRepository(read, principal.tenant_id).get(pending.id) is None
            rows = await AuditEventRepository(read, principal.tenant_id).list_recent()
            assert len(rows) == 1
            row = rows[0]
            assert (row.action, row.outcome.value) == ("permission.denied", "denied")
            assert (row.actor_id, row.tenant_id) == (principal.user_id, principal.tenant_id)
            assert (row.resource_type, row.resource_id) == (resource_type, str(target))
            assert (row.request_id, row.source_origin, str(row.source_ip)) == (
                "r9-v2-denial",
                "client",
                "203.0.113.91",
            )
            assert row.metadata == {"attempted_action": action, "reason": "not_visible"}


@pytest.mark.parametrize(
    "kind",
    [
        "collection_update",
        "collection_delete",
        "saved_update",
        "saved_delete",
        "chat_update",
        "chat_delete",
    ],
)
async def test_late_target_absence_has_one_durable_denial(live, monkeypatch, kind):  # noqa: F811
    principal = live.principal
    family, mutation = kind.split("_")
    async with live.factory() as seed:
        await bind_tenant(seed, principal.tenant_id)
        if family == "collection":
            resource = await CollectionRepository(seed, principal.tenant_id).create(
                owner_id=principal.user_id, name="race"
            )
            repository_type = CollectionRepository
            base_path, resource_type, action_prefix = "collections", "collection", "collection"
        elif family == "chat":
            resource = await ChatSessionRepository(seed, principal.tenant_id).create(
                owner_id=principal.user_id, model="test-model", title="race"
            )
            repository_type = ChatSessionRepository
            base_path, resource_type, action_prefix = (
                "chat/sessions",
                "chat_session",
                "chat.session",
            )
        else:
            resource = await SavedSearchRepository(seed, principal.tenant_id).create(
                owner_id=principal.user_id, name="race", query="question"
            )
            repository_type = SavedSearchRepository
            base_path, resource_type, action_prefix = (
                "saved-searches",
                "saved_search",
                "saved_search",
            )
        await seed.commit()

    # #606's upload cleanup locks the parent before delete_owned. Compete before
    # that first lock; the separate R6 test proves later FK inserts are blocked.
    method = "get" if kind == "collection_delete" else mutation
    path = f"/api/v1/{base_path}/{resource.id}"
    action = f"{action_prefix}.{mutation}"
    entered, release = asyncio.Event(), asyncio.Event()
    original = getattr(repository_type, method)
    application = create_app()
    application.dependency_overrides[get_durable_audit_transactions_dep] = lambda: live.transactions
    application.dependency_overrides[get_object_store] = lambda: _Store()
    async with live.factory() as caller:
        await bind_tenant(caller, principal.tenant_id)
        pending = await CollectionRepository(caller, principal.tenant_id).create(
            owner_id=principal.user_id, name="must roll back"
        )

        async def caller_session():
            yield caller

        application.dependency_overrides[get_db_session] = caller_session

        async def pause(repo, *args, **kwargs):
            if repo._session is caller:
                entered.set()
                await release.wait()
            return await original(repo, *args, **kwargs)

        monkeypatch.setattr(repository_type, method, pause)
        token = mint_access_token(principal, get_settings()).token
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, client=("203.0.113.90", 40000)),
            base_url="http://test",
            headers={"authorization": f"Bearer {token}", "x-request-id": f"r7-{kind}"},
        ) as client:
            task = asyncio.create_task(
                client.delete(path)
                if mutation == "delete"
                else client.patch(
                    path, json={"title": "edited"} if family == "chat" else {"name": "edited"}
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 30)
                # A commits after B's ownership SELECT, before B's real mutation.
                async with live.factory() as competitor:
                    await bind_tenant(competitor, principal.tenant_id)
                    assert await repository_type(competitor, principal.tenant_id).delete(
                        resource.id
                    )
                    await competitor.commit()
                release.set()
                response = await asyncio.wait_for(task, 30)
                assert response.status_code == 404, response.text
                await caller.rollback()
                async with live.factory() as read:
                    await bind_tenant(read, principal.tenant_id)
                    assert (
                        await CollectionRepository(read, principal.tenant_id).get(pending.id)
                        is None
                    )
                    rows = await AuditEventRepository(read, principal.tenant_id).list_recent()
                assert len(rows) == 1
                row = rows[0]
                assert (row.action, row.outcome.value) == ("permission.denied", "denied")
                assert (row.actor_id, row.tenant_id) == (principal.user_id, principal.tenant_id)
                assert (row.resource_type, row.resource_id) == (resource_type, str(resource.id))
                assert row.request_id == f"r7-{kind}"
                assert (row.source_origin, str(row.source_ip)) == ("client", "203.0.113.90")
                assert row.metadata == {"attempted_action": action, "reason": "not_visible"}
                # A healthy initial-miss control also produces exactly one row.
                control = await client.get(f"/api/v1/{base_path}/{uuid.uuid4()}")
                assert control.status_code == 404
                await caller.rollback()
                async with live.factory() as read:
                    await bind_tenant(read, principal.tenant_id)
                    assert (
                        len(await AuditEventRepository(read, principal.tenant_id).list_recent())
                        == 2
                    )
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
