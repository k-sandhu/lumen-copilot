"""R8-001/R9: withhold assistant turns with any forbidden stored dependency."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update

from app.db import models
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    MessageRepository,
    SessionSummaryRepository,
)
from app.domain.entities import GrantPrincipalType, GrantResourceType, GrantRole, MessageRole
from app.retrieval.service import RetrievalService
from app.services.tools.impls.recall import _read_conversation
from app.services.tools.types import RecallOutcome, ToolContext
from app.services.transcript_recall import SessionTranscriptReader
from tests.test_transcript_recall import _Ctx, _ctx_for, _turn, ctx  # noqa: F401


async def assert_overlapping_mentions_are_withheld(
    session, principal, session_id, permitted_id, stranger_id, *, permitted_name, revoked_name, body
) -> None:
    await session.execute(
        update(models.Document)
        .where(models.Document.id == permitted_id)
        .values(filename=permitted_name)
    )
    collection = await CollectionRepository(session, principal.tenant_id).create(
        owner_id=stranger_id, name="private-overlap"
    )
    document = await DocumentRepository(session, principal.tenant_id).create(
        owner_id=stranger_id,
        collection_id=collection.id,
        filename=revoked_name,
        mime_type="text/plain",
        size_bytes=1,
        storage_key="r9-overlap",
        acl_enforced=False,
    )
    grant = await GrantRepository(session, principal.tenant_id).create(
        resource_type=GrantResourceType.DOCUMENT,
        resource_id=document.id,
        principal_type=GrantPrincipalType.USER,
        principal_id=principal.user_id,
        role=GrantRole.VIEWER,
        granted_by=stranger_id,
    )
    message_id = uuid.uuid4()
    stamp = datetime(2020, 1, 1, tzinfo=UTC)
    await MessageRepository(session, principal.tenant_id).add_with_id(
        message_id=message_id,
        session_id=session_id,
        role=MessageRole.ASSISTANT,
        content=body,
        source_document_ids=[],
    )
    await session.execute(
        update(models.Message).where(models.Message.id == message_id).values(created_at=stamp)
    )
    await SessionSummaryRepository(session, principal.tenant_id).upsert_summary(
        session_id,
        summary="Earlier discussion",
        covers_through_message_id=message_id,
        covered_created_at=stamp,
        mentioned_documents={permitted_id: permitted_name, document.id: revoked_name},
    )
    await session.commit()
    retrieval = RetrievalService(session, gateway=object())

    async def recall(query):
        return await _read_conversation(
            {"query": query, "k": 1},
            ToolContext(
                principal=principal,
                retrieval=retrieval,
                transcript=SessionTranscriptReader(
                    session=session,
                    principal=principal,
                    session_id=session_id,
                    compaction_cursor=(stamp, message_id),
                ),
            ),
        )

    granted = await recall("NAVIGATION")
    assert body in granted.content, "permitted assistant prose is returned unchanged"
    assert granted.source_document_ids == tuple(sorted((permitted_id, document.id), key=str))
    await session.execute(delete(models.Grant).where(models.Grant.id == grant.id))
    await session.commit()
    assert await retrieval.get_document(principal=principal, document_id=document.id) is None
    assert (
        await retrieval.permitted_document_names(principal=principal, document_ids=[document.id])
        == {}
    )
    positive = await recall("ORION")
    absent = await recall("LYRA")
    assert positive == absent, "forbidden overlapping mentions must not affect any result field"
    assert positive.content.encode() == absent.content.encode()
    navigation = await recall("NAVIGATION")
    assert navigation == absent, "withhold the whole turn, including unrelated permitted prose"
    assert navigation.source_document_ids == ()
    assert navigation.hit_count == 0
    assert navigation.document_ids == navigation.passages == ()
    stored = await session.get(models.Message, message_id)
    assert stored.content == body and stored.source_document_ids == []


OVERLAP_CASES = [
    ("Project Plan", "Plan for ORION.pdf", "NAVIGATION: Project Plan for ORION.pdf"),
    ("Plan.pdf", "ORION Plan.pdf", "NAVIGATION: ORION Plan.pdf"),
]


@pytest.mark.parametrize("permitted_name,revoked_name,body", OVERLAP_CASES)
async def test_overlapping_permitted_and_revoked_mentions_withhold_whole_turn(
    ctx: _Ctx,  # noqa: F811 — imported shared fixture
    permitted_name: str,
    revoked_name: str,
    body: str,  # noqa: F811
) -> None:
    async with ctx.sessionmaker() as session:
        await assert_overlapping_mentions_are_withheld(
            session,
            ctx.principal(),
            ctx.session_id,
            ctx.document_id,
            ctx.stranger_id,
            permitted_name=permitted_name,
            revoked_name=revoked_name,
            body=body,
        )


@pytest.mark.parametrize("body", ["NAVIGATION unrelated prose", ""])
async def test_handler_withholds_stored_mention_without_scanning_rendered_text(body: str) -> None:
    permitted, revoked = uuid.UUID(int=1), uuid.UUID(int=2)
    outcome = RecallOutcome(
        turns=(_turn(body, cited=(permitted,), mentioned=(revoked,)),),
        # No visible name or map: stored IDs alone must govern permission.
    )
    context, retrieval = _ctx_for(outcome, permits={permitted: "allowed.pdf"})
    actual = await _read_conversation({"query": "NAVIGATION"}, context)
    empty_context, _ = _ctx_for(RecallOutcome())
    expected = await _read_conversation({"query": "LYRA"}, empty_context)
    assert actual == expected
    assert retrieval.asked == [[permitted, revoked]]


@pytest.mark.parametrize("role", ["assistant", "user"])
async def test_permitted_prose_and_user_words_are_returned_as_is(role: str) -> None:
    document = uuid.UUID(int=1)
    body = "  Project Plan for ORION.pdf [document no longer accessible]  "
    turn = _turn(body, role=role)
    if role == "user":
        # The caller's words survive even UNKNOWN provenance and revoked IDs.
        turn = replace(
            turn,
            cited_document_ids=(document,),
            mentioned_document_ids=(document,),
            provenance_known=False,
        )
    context, retrieval = _ctx_for(RecallOutcome(turns=(turn,)))
    result = await _read_conversation({}, context)
    assert "\n" + body + "\n" in result.content
    assert result.hit_count == 1 and result.source_document_ids == ()
    assert retrieval.asked == []
