"""PR570 rebase: media citations retain durable recall dependencies (#606/#569)."""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import delete, text

from app.auth.principal import Principal
from app.db import models
from app.db.repositories import (
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CitationRepository,
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    MessageRepository,
    TenantRepository,
    TranscriptRepository,
    TranscriptSegmentInput,
    TranscriptSpeakerInput,
    UserRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.chat import GroundedCitation
from app.domain.entities import DocumentKind, GrantPrincipalType, GrantResourceType, GrantRole, Role
from app.domain.retrieval import RetrievedPassage
from app.retrieval.service import RetrievalService
from app.services.chat_runtime import ChatRuntime, _citation_event_data
from app.services.tools.impls.recall import _read_conversation
from app.services.tools.types import ToolContext
from app.services.transcript_recall import SessionTranscriptReader
from tests._live_helpers import isolated_live_url
from tests.test_chat_runtime import _Ctx, _FakeRetrieval
from tests.test_chat_runtime import ctx as media_ctx  # noqa: F401
from tests.test_transcript_recall_postgres import factory_and_role  # noqa: F401


async def _media_passage(session: Any, tenant_id: uuid.UUID, document_id: uuid.UUID):
    body = "The confidential margin was 41%."
    segment_id = uuid.uuid4()
    await DocumentRepository(session, tenant_id).update_media_metadata(
        document_id,
        kind=DocumentKind.AUDIO,
        duration_ms=10_000,
        transcript_language="en",
        transcription_model="test",
    )
    await TranscriptRepository(session, tenant_id).replace_for_document(
        document_id,
        speakers=[TranscriptSpeakerInput(speaker_id="speaker-1")],
        segments=[
            TranscriptSegmentInput(
                id=segment_id,
                ordinal=0,
                speaker_id="speaker-1",
                start_ms=1_000,
                end_ms=4_000,
                char_start=0,
                char_end=len(body),
                text=body,
            )
        ],
    )
    chunk = (
        await ChunkRepository(session, tenant_id).replace_for_document(
            document_id,
            [
                ChunkInput(
                    text=body,
                    char_start=0,
                    char_end=len(body),
                    time_start_ms=1_000,
                    time_end_ms=4_000,
                    transcript_segment_id=segment_id,
                    speaker_id="speaker-1",
                )
            ],
        )
    )[0]
    return RetrievedPassage(
        chunk_id=chunk.id,
        document_id=document_id,
        document_name="meeting.wav",
        ord=0,
        text=body,
        char_start=0,
        char_end=len(body),
        score=1.0,
        time_start_ms=1_000,
        time_end_ms=4_000,
        transcript_segment_id=segment_id,
        speaker_id="speaker-1",
    )


async def _check_media_recall(factory, principal, session_id, passage, retrieval_factory, revoke):
    """Persist through the merged citation path, then recall under current permissions."""
    message_id = uuid.uuid4()
    async with factory() as session:
        stored = await ChatRuntime.__new__(ChatRuntime)._persist(
            session=session,
            tenant_id=principal.tenant_id,
            session_id=session_id,
            assistant_message_id=message_id,
            model="test",
            content=passage.text,
            citations=[GroundedCitation.from_passage(passage)],
            prompt_context=[],
        )
        await session.commit()
        payload = _citation_event_data(stored[0])
        assert payload["timeStartMs"] == 1_000
        assert payload["timeEndMs"] == 4_000
        assert payload["transcriptSegmentId"] == str(passage.transcript_segment_id)
        assert payload["speakerId"] == "speaker-1"
    async with factory() as session:
        hydrated = await CitationRepository(session, principal.tenant_id).list_for_message_hydrated(
            message_id
        )
        assert hydrated[0].time_start_ms == 1_000
        assert hydrated[0].transcript_segment_id == passage.transcript_segment_id
        message = await MessageRepository(session, principal.tenant_id).get(message_id)
        assert message.source_document_ids == (passage.document_id,)
        cursor = (message.created_at, message_id)

    async def recall():
        async with factory() as session:
            return await _read_conversation(
                {"query": "41%"},
                ToolContext(
                    principal=principal,
                    retrieval=retrieval_factory(session),
                    transcript=SessionTranscriptReader(
                        session=session,
                        principal=principal,
                        session_id=session_id,
                        compaction_cursor=cursor,
                    ),
                ),
            )

    permitted = await recall()
    assert passage.text in permitted.content
    assert permitted.source_document_ids == (passage.document_id,)
    assert permitted.passages == permitted.document_ids == ()
    await revoke()
    # The recall guard must rely on the immutable snapshot, even after the
    # media citation disappears; no optimistic surviving-citation fallback.
    async with factory() as session:
        await session.execute(
            delete(models.Citation).where(models.Citation.message_id == message_id)
        )
        await session.commit()
    async with factory() as session:
        sources = await MessageRepository(
            session, principal.tenant_id
        ).source_documents_for_messages([message_id])
        assert sources[message_id] == (passage.document_id,)
    revoked = await recall()
    assert passage.text not in revoked.content
    assert "No earlier turn matches" in revoked.content
    assert revoked.source_document_ids == ()
    assert "withheld_turns" not in revoked.payload


async def test_media_citation_recall_uses_stored_provenance(media_ctx: _Ctx) -> None:  # noqa: F811
    async with media_ctx.sessionmaker() as session:
        passage = await _media_passage(session, media_ctx.tenant_id, media_ctx.document_id)
        await session.commit()

    class Permissions(_FakeRetrieval):
        permitted = True

        async def permitted_document_names(self, **kwargs):
            return {passage.document_id: "meeting.wav"} if self.permitted else {}

    retrieval = Permissions([])

    async def revoke():
        retrieval.permitted = False

    await _check_media_recall(
        media_ctx.sessionmaker,
        media_ctx.principal,
        media_ctx.session_id,
        passage,
        lambda session: retrieval,
        revoke,
    )


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1"
    or os.environ.get("DATABASE_URL")
    != isolated_live_url(
        "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumentest_pr570"
    ),
    reason="requires RUN_LIVE=1 and the isolated lumentest_pr570 DATABASE_URL",
)
async def test_media_recall_obeys_postgres_grant_revocation(factory_and_role):  # noqa: F811
    factory, role = factory_and_role
    async with factory() as session:
        tenant = await TenantRepository(session).create(name="pr570-media")
        users = UserRepository(session, tenant.id)
        alice = await users.create(email="alice@media.test", password_hash="x", roles=[Role.MEMBER])
        bob = await users.create(email="bob@media.test", password_hash="x", roles=[Role.MEMBER])
        chat = await ChatSessionRepository(session, tenant.id).create(
            owner_id=alice.id, model="test"
        )
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=bob.id, name="c"
        )
        document = await DocumentRepository(session, tenant.id).create(
            owner_id=bob.id,
            collection_id=collection.id,
            filename="meeting.wav",
            mime_type="audio/wav",
            size_bytes=100,
            storage_key=f"{tenant.id}/meeting.wav",
            acl_enforced=False,
            kind=DocumentKind.AUDIO,
        )
        passage = await _media_passage(session, tenant.id, document.id)
        await GrantRepository(session, tenant.id).create(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=document.id,
            principal_type=GrantPrincipalType.USER,
            principal_id=alice.id,
            role=GrantRole.VIEWER,
            granted_by=bob.id,
        )
        await session.commit()
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

    async def revoke():
        async with scoped() as session:
            await session.execute(
                delete(models.Grant).where(models.Grant.resource_id == document.id)
            )
            await session.commit()

    await _check_media_recall(
        scoped,
        principal,
        chat.id,
        passage,
        lambda session: RetrievalService(session, gateway=object()),
        revoke,
    )
