"""R7-001: stored dependencies survive every recall clipping boundary."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, update

from app.db import models
from app.retrieval.service import RetrievalService
from app.services.tools.impls.recall import _read_conversation
from app.services.tools.types import RecallOutcome, ToolContext
from tests.test_transcript_recall import _Ctx, _ctx_for, _turn, ctx  # noqa: F401


@pytest.mark.parametrize("tail", ["straddling", "beyond", "zero", "revoked"])
async def test_repository_reader_handler_keep_stored_mentions(
    ctx: _Ctx,  # noqa: F811 — imported shared fixture
    monkeypatch: pytest.MonkeyPatch,
    tail: str,
) -> None:
    import app.services.tools.impls.recall as recall

    name = "Confidential-Q3-Project-ORION-Plan.pdf"
    padding = 565 if tail != "beyond" else 800
    body = "NAVIGATION " + "x" * padding + " " + name
    async with ctx.sessionmaker() as session:
        await session.execute(
            update(models.Document)
            .where(models.Document.id == ctx.document_id)
            .values(filename=name)
        )
        await session.execute(
            update(models.Message)
            .where(models.Message.id == ctx.message_ids[5])
            .values(content=body)
        )
        await ctx.set_cursor(session, index=6, summary="old talks")
        await session.execute(
            update(models.SessionSummary)
            .where(models.SessionSummary.session_id == ctx.session_id)
            .values(mentioned_documents={str(ctx.document_id): name})
        )
        if tail == "revoked":
            await session.execute(
                delete(models.Document).where(models.Document.id == ctx.document_id)
            )
        await session.commit()
        rendered = []
        original_render = recall._render_turn

        def render(turn, *, text, budget):
            rendered.append(text)
            return original_render(turn, text=text, budget=budget)

        monkeypatch.setattr(recall, "_render_turn", render)
        result = await _read_conversation(
            {"query": "NAVIGATION", "k": 1},
            ToolContext(
                principal=ctx.principal(),
                retrieval=RetrievalService(session, gateway=object()),
                transcript=ctx.reader(session),
                snippet_budget=1 if tail == "zero" else 600,
            ),
        )
        assert result.ok
        if tail == "revoked":
            assert result.hit_count == 0 and result.source_document_ids == ()
            assert rendered == [] and "NAVIGATION" not in result.content
            return
        assert result.hit_count == 1
        assert result.source_document_ids == (
            ctx.document_id,
        ), "stored mention lost during clipping"
        assert result.document_ids == result.passages == ()
        assert rendered == [
            body[:599] + "…" if len(body) > 600 else body
        ], "permission checks precede simple display clipping"
        if tail == "zero":
            assert "NAVIGATION" not in result.content, "no original characters survive this budget"
        stored = await session.get(models.Message, ctx.message_ids[5])
        assert stored.content == body and stored.source_document_ids == []


async def test_zero_character_turn_keeps_both_stored_dependency_sets() -> None:
    source, mention = uuid.UUID(int=1), uuid.UUID(int=2)
    outcome = RecallOutcome(turns=(_turn("", cited=(source,), mentioned=(mention,)),))
    context, retrieval = _ctx_for(outcome, permits={source: "source.pdf", mention: "mention.pdf"})
    result = await _read_conversation({}, context)
    assert result.hit_count == 1
    assert result.source_document_ids == (source, mention)
    assert retrieval.asked == [[source, mention]], "permissions follow IDs, never rendered text"
    assert result.passages == result.document_ids == ()
