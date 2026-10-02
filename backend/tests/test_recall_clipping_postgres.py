"""R7-001: repository-to-runtime recall dependencies survive real grant revocation."""

import sys
import types
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, text, update

from app.api.v1.chat import _to_chat_messages
from app.auth.principal import Principal
from app.core.config import get_settings
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
from app.domain.llm import Role as LlmRole
from app.domain.llm import StreamEvent, ToolCall
from app.llm.context import ContextConfig
from app.realtime.backplane import InMemoryBackplane
from app.retrieval.service import RetrievalService
from app.services.assistant_runtime import AssistantRunConfig
from app.services.chat_runtime import ChatRuntime
from app.services.chat_service import ChatService
from app.services.tools.impls.recall import _read_conversation
from app.services.tools.types import ToolContext
from app.services.transcript_recall import SessionTranscriptReader
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_context
from tests.test_chat_runtime import _drain
from tests.test_transcript_recall_postgres import factory_and_role, pytestmark  # noqa: F401


async def test_clipped_name_dependency_blocks_runtime_replay_after_revocation(
    factory_and_role,  # noqa: F811 — imported shared fixture
    monkeypatch,
):
    factory, role = factory_and_role
    stamp = datetime(2021, 1, 1, tzinfo=UTC)
    original = uuid.uuid4()
    name = "Confidential-Q3-Project-ORION-Plan.pdf"
    original_body = "NAVIGATION " + "x" * 565 + " " + name
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="independent-r7")
        users = UserRepository(seed, tenant.id)
        alice = await users.create(email="alice@r7.test", password_hash="x", roles=[Role.MEMBER])
        bob = await users.create(email="bob@r7.test", password_hash="x", roles=[Role.MEMBER])
        chat = await ChatSessionRepository(seed, tenant.id).create(
            owner_id=alice.id, model="review/unknown"
        )
        coll = await CollectionRepository(seed, tenant.id).create(owner_id=bob.id, name="private")
        doc = await DocumentRepository(seed, tenant.id).create(
            owner_id=bob.id,
            collection_id=coll.id,
            filename=name,
            mime_type="text/plain",
            size_bytes=30,
            storage_key=f"{tenant.id}/private",
            acl_enforced=False,
            status=DocumentStatus.READY,
        )
        await GrantRepository(seed, tenant.id).create(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=doc.id,
            principal_type=GrantPrincipalType.USER,
            principal_id=alice.id,
            role=GrantRole.VIEWER,
            granted_by=bob.id,
        )
        # Same supported known-empty/uncited-name state used by R1-002 controls.
        # Names have summary mention dependencies independently of citation provenance.
        await MessageRepository(seed, tenant.id).add_with_id(
            message_id=original,
            session_id=chat.id,
            role=MessageRole.ASSISTANT,
            content=original_body,
            source_document_ids=[],
        )
        await seed.execute(
            update(models.Message).where(models.Message.id == original).values(created_at=stamp)
        )
        await SessionSummaryRepository(seed, tenant.id).upsert_summary(
            chat.id,
            summary="considerations " * 300,
            covers_through_message_id=original,
            covered_created_at=stamp,
            mentioned_documents={doc.id: name},
        )
        await seed.commit()
    principal = Principal(user_id=alice.id, tenant_id=tenant.id, roles=(Role.MEMBER,))

    @asynccontextmanager
    async def scoped():
        async with factory() as session:
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await bind_tenant(session, tenant.id)
            yield session

    async with scoped() as session:
        flags = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert not flags.rolsuper and not flags.rolbypassrls
        retrieval = RetrievalService(session, gateway=object())
        assert await retrieval.get_document(principal=principal, document_id=doc.id) is not None
        initial = await _read_conversation(
            {"query": "NAVIGATION", "k": 1},
            ToolContext(
                principal=principal,
                retrieval=retrieval,
                transcript=SessionTranscriptReader(
                    session=session,
                    principal=principal,
                    session_id=chat.id,
                    compaction_cursor=(stamp, original),
                ),
            ),
        )
        assert "NAVIGATION" in initial.content
        assert initial.source_document_ids == (doc.id,), "repository clipping lost stored mention"
        sent = await ChatService(
            session,
            tenant_id=tenant.id,
            owner_id=alice.id,
            settings=get_settings(),
            denials=denial_context(
                RecordingDurableAuditTransactions(), session, tenant.id, alice.id
            ),
        ).send_message(
            chat.id,
            content="Repeat the start of the earlier filename, using NAVIGATION.",
            model=None,
            backplane=InMemoryBackplane(),
        )
        await session.commit()
        assert sent.compaction_cursor == (stamp, original)
        assert all(m.id != original for m in sent.history)

    copied_body = "The earlier filename started Confidential-Q3-Project."

    class CopyGateway:
        calls = 0

        async def stream_tools(self, messages, **kwargs):
            assert not any(
                "considerations considerations" in m.content for m in messages
            ), "normal assembly must shed summary"
            self.calls += 1
            if self.calls == 1:
                assert not any("Confidential-Q3" in m.content for m in messages)
                yield StreamEvent(
                    tool_calls=(
                        ToolCall(
                            id="recall",
                            name="read_conversation",
                            arguments={"query": "NAVIGATION", "k": 1},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                replies = [m for m in messages if m.role == LlmRole.TOOL]
                assert len(replies) == 1 and "Confidential-Q3" in replies[0].content
                assert replies[0].source_document_ids == (doc.id,)
                yield StreamEvent(text=copied_body)
                yield StreamEvent(finish_reason="stop")

    def unavailable(**kwargs):
        raise RuntimeError("offline tokenizer")

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        types.SimpleNamespace(token_counter=unavailable, get_model_info=unavailable),
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    gateway = CopyGateway()
    runtime = ChatRuntime(
        sessionmaker=scoped,
        gateway=gateway,
        backplane=backplane,
        principal=principal,
        request_id="independent-r7",
        source_ip="unknown",
        suggestions_enabled=False,
        tool_concurrency=1,
        retrieval_factory=lambda session: RetrievalService(session, gateway=object()),
        context_config=ContextConfig(fallback_max_input_tokens=7000, output_headroom_tokens=0),
    )
    runtime._token_counters["review/unknown"] = len
    await runtime.run(
        stream_id=stream_id,
        session_id=chat.id,
        question=sent.user_message.content,
        model="review/unknown",
        history=_to_chat_messages(sent.history),
        collection_ids=None,
        summary=sent.summary,
        evidence=sent.evidence,
        mentioned_documents=sent.mentioned_documents,
        compaction_cursor=sent.compaction_cursor,
        assistant_config=AssistantRunConfig(
            system_prompt="Be grounded.",
            allowed=frozenset({"read_conversation"}),
            collection_ids=None,
            model=None,
        ),
    )
    envelopes = await _drain(backplane, stream_id)
    assert envelopes[-1]["type"] == "done", envelopes[-1]
    assert gateway.calls == 2
    copied_id = uuid.UUID(envelopes[-1]["data"]["messageId"])
    later = stamp + timedelta(minutes=2)
    async with scoped() as session:
        copied = await MessageRepository(session, tenant.id).get(copied_id)
        assert copied.content == copied_body and copied.source_document_ids == (doc.id,)
        await session.execute(
            update(models.Message).where(models.Message.id == copied_id).values(created_at=later)
        )
        await SessionSummaryRepository(session, tenant.id).upsert_summary(
            chat.id,
            summary="Earlier discussion",
            covers_through_message_id=copied_id,
            covered_created_at=later,
            mentioned_documents={doc.id: name},
        )
        await session.execute(delete(models.Grant).where(models.Grant.resource_id == doc.id))
        await session.commit()
    async with scoped() as session:
        retrieval = RetrievalService(session, gateway=object())
        assert await retrieval.get_document(principal=principal, document_id=doc.id) is None
        assert (
            await retrieval.permitted_document_names(principal=principal, document_ids=[doc.id])
            == {}
        )
        after = await _read_conversation(
            {"query": "Confidential-Q3"},
            ToolContext(
                principal=principal,
                retrieval=retrieval,
                transcript=SessionTranscriptReader(
                    session=session,
                    principal=principal,
                    session_id=chat.id,
                    compaction_cursor=(later, copied_id),
                ),
            ),
        )
        assert copied_body not in after.content
        assert "No earlier turn matches" in after.content
        assert after.source_document_ids == ()
        assert after.passages == after.document_ids == ()
