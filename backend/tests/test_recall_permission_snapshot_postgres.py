"""R10-001: selection and rendering share one permission snapshot per recall."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, text, update

from app.auth.principal import Principal
from app.db import models
from app.db.repositories import (
    ChatSessionRepository,
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    MessageRepository,
    SessionSummaryRepository,
    TenantRepository,
    UserRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.entities import (
    DocumentStatus,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    MessageRole,
    Role,
)
from app.retrieval.service import RetrievalService
from app.services.tools.impls.recall import _read_conversation
from app.services.tools.types import ToolContext
from app.services.transcript_recall import SessionTranscriptReader
from tests.test_transcript_recall_postgres import factory_and_role, pytestmark  # noqa: F401


@pytest.mark.parametrize("boundary", ["mention_preselection", "source_postselection"])
async def test_committed_revocation_uses_one_permission_snapshot(
    factory_and_role,  # noqa: F811 — imported shared fixture
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    factory, role = factory_and_role
    base = datetime(2021, 1, 1, tzinfo=UTC)
    safe_id, dependent_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="pr570-r11-race")
        users = UserRepository(seed, tenant.id)
        alice = await users.create(email="alice@r11.test", password_hash="x", roles=[Role.MEMBER])
        bob = await users.create(email="bob@r11.test", password_hash="x", roles=[Role.MEMBER])
        chat = await ChatSessionRepository(seed, tenant.id).create(owner_id=alice.id, model="m")
        collection = await CollectionRepository(seed, tenant.id).create(
            owner_id=bob.id, name="private"
        )
        doc = await DocumentRepository(seed, tenant.id).create(
            owner_id=bob.id,
            collection_id=collection.id,
            filename="secret-ORION.pdf",
            mime_type="text/plain",
            size_bytes=1,
            storage_key="pr570-r11-secret",
            acl_enforced=False,
            status=DocumentStatus.READY,
        )
        messages = MessageRepository(seed, tenant.id)
        await messages.add_with_id(
            message_id=safe_id,
            session_id=chat.id,
            role=MessageRole.ASSISTANT,
            content="NAVIGATION ORION LYRA safe fallback",
            source_document_ids=[],
        )
        await messages.add_with_id(
            message_id=dependent_id,
            session_id=chat.id,
            role=MessageRole.ASSISTANT,
            content="NAVIGATION secret-ORION.pdf confidential",
            source_document_ids=[] if boundary == "mention_preselection" else [doc.id],
        )
        await seed.execute(
            update(models.Message).where(models.Message.id == safe_id).values(created_at=base)
        )
        await seed.execute(
            update(models.Message)
            .where(models.Message.id == dependent_id)
            .values(created_at=base + timedelta(seconds=2))
        )
        await SessionSummaryRepository(seed, tenant.id).upsert_summary(
            chat.id,
            summary="Safe summary",
            covers_through_message_id=dependent_id,
            covered_created_at=base + timedelta(seconds=2),
            mentioned_documents={doc.id: doc.filename}
            if boundary == "mention_preselection"
            else {},
        )
        await seed.commit()
    principal = Principal(user_id=alice.id, tenant_id=tenant.id, roles=(Role.MEMBER,))

    @asynccontextmanager
    async def scoped():
        async with factory() as session:
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await bind_tenant(session, tenant.id)
            flags = (
                await session.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            ).one()
            assert not flags.rolsuper and not flags.rolbypassrls
            yield session

    async def grant() -> None:
        async with scoped() as writer:
            await GrantRepository(writer, tenant.id).create(
                resource_type=GrantResourceType.DOCUMENT,
                resource_id=doc.id,
                principal_type=GrantPrincipalType.USER,
                principal_id=alice.id,
                role=GrantRole.VIEWER,
                granted_by=bob.id,
            )
            await writer.commit()

    async def revoke() -> None:
        async with scoped() as writer:
            await writer.execute(delete(models.Grant).where(models.Grant.resource_id == doc.id))
            await writer.commit()

    async def recall(query: str, *, race: bool = False, pre_revoked: bool = False) -> bytes:
        await grant()
        if pre_revoked:
            await revoke()
        reached_boundary, committed = asyncio.Event(), asyncio.Event()

        async def revoker() -> None:
            await reached_boundary.wait()
            await revoke()
            committed.set()

        async with scoped() as session:
            original_execute = session.execute
            triggered = False

            async def execute(stmt, *args, **kwargs):
                nonlocal triggered
                result = await original_execute(stmt, *args, **kwargs)
                desc = getattr(stmt, "column_descriptions", [])
                # The old mention-permission read and the new single snapshot
                # read are the same boundary: permission SELECT has completed,
                # candidate SELECT has not started. This keeps the revert check
                # load-bearing across removal of the old mention-only query.
                is_permissions = bool(
                    desc
                    and (
                        desc[0].get("name") == "value"
                        or (
                            desc[0].get("entity") is models.Document and desc[0].get("name") == "id"
                        )
                    )
                )
                is_candidates = bool(desc and desc[0].get("entity") is models.Message)
                boundary_hit = (
                    is_permissions if boundary == "mention_preselection" else is_candidates
                )
                if race and not triggered and boundary_hit:
                    triggered = True
                    reached_boundary.set()
                    await committed.wait()
                return result

            monkeypatch.setattr(session, "execute", execute)
            task = asyncio.create_task(revoker()) if race else None
            try:
                result = await _read_conversation(
                    {"query": query, "k": 1},
                    ToolContext(
                        principal=principal,
                        retrieval=RetrievalService(session, gateway=object()),
                        transcript=SessionTranscriptReader(
                            session=session,
                            principal=principal,
                            session_id=chat.id,
                            compaction_cursor=(base + timedelta(seconds=2), dependent_id),
                        ),
                    ),
                )
                if race:
                    assert triggered and committed.is_set(), "revocation boundary was not exercised"
                    await task
                    # A fresh transaction proves the revocation really committed;
                    # this is not a stubbed retrieval answer or an uncommitted write.
                    async with scoped() as observer:
                        assert (
                            await RetrievalService(
                                observer, gateway=object()
                            ).permitted_document_names(principal=principal, document_ids=[doc.id])
                            == {}
                        )
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        await revoke()
        # Include every field, including metadata and internal provenance, in
        # the byte comparison rather than checking just displayed text.
        return json.dumps(asdict(result), default=str, sort_keys=True).encode()

    before = {query: await recall(query) for query in ("NAVIGATION ORION", "NAVIGATION LYRA")}
    assert b"confidential" in before["NAVIGATION ORION"]
    assert str(doc.id).encode() in before["NAVIGATION ORION"]
    after = {
        query: await recall(query, pre_revoked=True)
        for query in ("NAVIGATION ORION", "NAVIGATION LYRA")
    }
    assert after["NAVIGATION ORION"] == after["NAVIGATION LYRA"]
    assert b"safe fallback" in after["NAVIGATION ORION"]
    assert b"confidential" not in after["NAVIGATION ORION"]
    for query in before:
        raced = await recall(query, race=True)
        assert raced in (
            before[query],
            after[query],
        ), "selection and final withholding must be byte-identical to one permission snapshot"
        assert raced == before[query], "revocation commits after this recall's permission snapshot"
