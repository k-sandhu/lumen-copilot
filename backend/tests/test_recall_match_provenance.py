"""R4-001: no keyword oracle, including runtime/persistence after summary shedding."""

from __future__ import annotations

import sys
import types
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, text, update

from app.api.v1.chat import _to_chat_messages
from app.auth.principal import Principal
from app.core.config import get_settings
from app.db import models
from app.db.repositories import (
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
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


@pytest.mark.live
async def test_revoked_search_cannot_inform_a_persisted_answer(factory_and_role, monkeypatch):  # noqa: F811
    factory, role = factory_and_role
    stamp = datetime(2021, 1, 1, tzinfo=UTC)
    original = uuid.uuid4()
    async with factory() as seed:
        tenant = await TenantRepository(seed).create(name="independent-r4")
        users = UserRepository(seed, tenant.id)
        alice = await users.create(
            email="alice@independent-r4.test", password_hash="x", roles=[Role.MEMBER]
        )
        bob = await users.create(
            email="bob@independent-r4.test", password_hash="x", roles=[Role.MEMBER]
        )
        chat = await ChatSessionRepository(seed, tenant.id).create(
            owner_id=alice.id, model="review/unknown"
        )
        coll = await CollectionRepository(seed, tenant.id).create(owner_id=bob.id, name="private")
        doc = await DocumentRepository(seed, tenant.id).create(
            owner_id=bob.id,
            collection_id=coll.id,
            filename="confidential.txt",
            mime_type="text/plain",
            size_bytes=30,
            storage_key=f"{tenant.id}/private",
            acl_enforced=False,
            status=DocumentStatus.READY,
        )
        await ChunkRepository(seed, tenant.id).replace_for_document(
            doc.id, [ChunkInput(text="The confidential code is ORION.", char_start=0, char_end=30)]
        )
        await GrantRepository(seed, tenant.id).create(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=doc.id,
            principal_type=GrantPrincipalType.USER,
            principal_id=alice.id,
            role=GrantRole.VIEWER,
            granted_by=bob.id,
        )
        await MessageRepository(seed, tenant.id).add_with_id(
            message_id=original,
            session_id=chat.id,
            role=MessageRole.ASSISTANT,
            content="The confidential code is ORION.",
            source_document_ids=[doc.id],
        )
        await seed.execute(
            update(models.Message).where(models.Message.id == original).values(created_at=stamp)
        )
        # A valid 300-word summary, with no secret. Tight-window assembly sheds it.
        await SessionSummaryRepository(seed, tenant.id).upsert_summary(
            chat.id,
            summary="discussion " * 300,
            covers_through_message_id=original,
            covered_created_at=stamp,
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
        retrieval = RetrievalService(session, gateway=object())
        assert await retrieval.get_document(principal=principal, document_id=doc.id) is not None
        await session.execute(delete(models.Grant).where(models.Grant.resource_id == doc.id))
        await session.commit()
    async with scoped() as session:
        flags = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        ).one()
        assert not flags.rolsuper and not flags.rolbypassrls
        retrieval = RetrievalService(session, gateway=object())
        assert await retrieval.get_document(principal=principal, document_id=doc.id) is None
        assert (
            await retrieval.permitted_document_names(principal=principal, document_ids=[doc.id])
            == {}
        )
        reader = SessionTranscriptReader(
            session=session,
            principal=principal,
            session_id=chat.id,
            compaction_cursor=(stamp, original),
        )
        context = ToolContext(principal=principal, retrieval=retrieval, transcript=reader)
        positive = await _read_conversation({"query": "ORION"}, context)
        negative = await _read_conversation({"query": "LYRA"}, context)
        assert positive == negative
        assert positive.content.encode() == negative.content.encode()
        assert "withheld_turns" not in positive.payload
        assert "No earlier turn matches" in negative.content
        assert positive.source_document_ids == negative.source_document_ids == ()
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
            content="Is the confidential code ORION or LYRA? Check the earlier discussion.",
            model=None,
            backplane=InMemoryBackplane(),
        )
        assert sent is not None
        await session.commit()
        assert sent.compaction_cursor == (stamp, original)
        assert all(m.id != original for m in sent.history)

    class MatchGateway:
        calls = 0

        async def stream_tools(self, messages, **kwargs):
            assert not any("The confidential code is ORION." in m.content for m in messages)
            assert not any(
                "discussion discussion" in m.content for m in messages
            ), "summary must be shed through normal assembly"
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    tool_calls=(
                        ToolCall(
                            id="positive", name="read_conversation", arguments={"query": "ORION"}
                        ),
                        ToolCall(
                            id="negative", name="read_conversation", arguments={"query": "LYRA"}
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                replies = {m.tool_call_id: m for m in messages if m.role == LlmRole.TOOL}
                assert replies["positive"].content == replies["negative"].content
                assert "No earlier turn matches" in replies["negative"].content
                assert (
                    replies["positive"].source_document_ids
                    == replies["negative"].source_document_ids
                    == ()
                )
                yield StreamEvent(
                    text="I could not confirm either code from the earlier discussion."
                )
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
    runtime = ChatRuntime(
        sessionmaker=scoped,
        gateway=MatchGateway(),
        backplane=backplane,
        principal=principal,
        request_id="independent-r4",
        source_ip="unknown",
        suggestions_enabled=False,
        tool_concurrency=1,
        retrieval_factory=lambda session: RetrievalService(session, gateway=object()),
        context_config=ContextConfig(fallback_max_input_tokens=4500, output_headroom_tokens=0),
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
    copied_id = uuid.UUID(envelopes[-1]["data"]["messageId"])
    async with scoped() as session:
        copied = await MessageRepository(session, tenant.id).get(copied_id)
        assert copied.content == "I could not confirm either code from the earlier discussion."
        assert (
            copied.source_document_ids == ()
        ), "only permitted inputs justify the known-empty answer snapshot"
        later = stamp + timedelta(minutes=2)
        await session.execute(
            update(models.Message).where(models.Message.id == copied_id).values(created_at=later)
        )
        await SessionSummaryRepository(session, tenant.id).upsert_summary(
            chat.id,
            summary="Earlier discussion",
            covers_through_message_id=copied_id,
            covered_created_at=later,
        )
        await session.commit()
    async with scoped() as session:
        recalled = await _read_conversation(
            {},
            ToolContext(
                principal=principal,
                retrieval=RetrievalService(session, gateway=object()),
                transcript=SessionTranscriptReader(
                    session=session,
                    principal=principal,
                    session_id=chat.id,
                    compaction_cursor=(later, copied_id),
                ),
            ),
        )
        assert "The confidential code is ORION" not in recalled.content
        assert copied.content in recalled.content
        assert recalled.source_document_ids == ()
        assert "withheld_turns" not in recalled.payload
