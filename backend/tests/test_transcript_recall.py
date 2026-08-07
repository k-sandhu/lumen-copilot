"""Conversation recall — ``read_conversation`` over the compacted range (#569).

The original chat is preserved; the *model* just cannot see it once the rolling
summariser folds a turn away. This covers the function that gives it back, in the
two halves it is built from:

* the **seam** (``SessionTranscriptReader``) against a real SQLite database — the
  owner predicate, the compacted-range bound, and the stopping rule;
* the **handler** (``read_conversation``) against fakes — the permission re-check
  on recalled evidence, the output bound, and the deliberate absence of citations.

The load-bearing test is :func:`test_the_compacted_range_and_the_live_window_partition_the_session`.
Everything else assumes recall reads exactly the turns the prompt does not; that
test is what makes the assumption true rather than intended.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db import models
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CitationRepository,
    CollectionRepository,
    DocumentRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import MessageRole, Role
from app.domain.llm import ChatMessage
from app.domain.llm import Role as LlmRole
from app.llm.context import ContextConfig, fit_transcript
from app.services.tools.impls.recall import (
    _MAX_TERMS,
    READ_CONVERSATION_TOOL_NAME,
    _read_conversation,
)
from app.services.tools.registry import default_allowlist, get_tool
from app.services.tools.types import RecalledTurn, RecallOutcome, ToolContext
from app.services.transcript_recall import SessionTranscriptReader

import app.db.models  # noqa: F401  isort: skip

#: The seeded conversation, as ``(seconds-from-base, id_int)`` per turn.
#:
#: Explicit rather than generated, because the cursor predicates this file exists
#: to pin have THREE branches and each needs its own shape. An evenly spaced
#: timeline exercises exactly one of them and lets the other two rot:
#:
#: * **Well separated** (0-5) — the plain inequality.
#: * **Inside the ±1s tolerance band** (6-8), with ids that DISAGREE with time
#:   order. This is the ordinary case, not a contrived one: message ids are
#:   UUID4, so id order and arrival order are independent. Turn 6 is 2.5s before
#:   turn 8 and carries the LARGEST id in the file — the one shape that makes a
#:   mis-sized tolerance window observable.
#: * **An exact same-second burst** (9-11) — the id tiebreaker.
#:
#: Both of the last two were found the hard way. A fixture of minute-spaced turns
#: let the tie branch be deleted outright with the suite green; adding a burst
#: fixed that but still let the window grow from 1s to 5s with the suite green,
#: because nothing sat between 1s and 5s of a cursor with a larger id. Widen the
#: spacing here and the partition test silently stops proving anything.
_TIMELINE: tuple[tuple[float, int], ...] = (
    (0.0, 10),
    (60.0, 20),
    (120.0, 30),
    (180.0, 40),
    (240.0, 50),
    (300.0, 60),
    (360.0, 95),  # earliest of the cluster, LARGEST id — the tolerance probe
    (360.4, 70),
    (362.5, 80),
    (420.0, 91),  # ─┐
    (420.0, 92),  #  ├ one timestamp, three rows: the tie branch
    (420.0, 93),  # ─┘
)
_TURNS = len(_TIMELINE)
#: Index of the message the summary's coverage cursor points at. Turns 0..6 are
#: compacted; 7..11 are still verbatim in the prompt.
_CURSOR = 6


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        owner_id: uuid.UUID,
        stranger_id: uuid.UUID,
        session_id: uuid.UUID,
        message_ids: list[uuid.UUID],
        document_id: uuid.UUID,
        chunk_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_id = tenant_id
        self.owner_id = owner_id
        self.stranger_id = stranger_id
        self.session_id = session_id
        self.message_ids = message_ids
        self.document_id = document_id
        self.chunk_id = chunk_id

    def principal(self, user_id: uuid.UUID | None = None) -> Principal:
        return Principal(
            user_id=user_id or self.owner_id,
            tenant_id=self.tenant_id,
            roles=(Role.MEMBER,),
        )

    def reader(
        self, session: AsyncSession, *, user_id: uuid.UUID | None = None, max_calls: int = 2
    ) -> SessionTranscriptReader:
        return SessionTranscriptReader(
            session=session,
            principal=self.principal(user_id),
            session_id=self.session_id,
            max_calls=max_calls,
        )

    async def set_cursor(self, session: AsyncSession, *, index: int, summary: str | None) -> None:
        """Point the summary's coverage cursor at ``message_ids[index]``.

        Written directly rather than through ``upsert_summary``: that method's
        forward-only CAS refuses to move a cursor backwards, and these tests need
        to place it anywhere — including at a row with NO summary text, the shape
        that proves the lockstep with ``send_message``.
        """
        existing = (
            await session.execute(
                select(models.SessionSummary).where(
                    models.SessionSummary.session_id == self.session_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = models.SessionSummary(
                tenant_id=self.tenant_id, session_id=self.session_id, version=1
            )
            session.add(existing)
        existing.summary = summary
        existing.covers_through_message_id = self.message_ids[index]
        existing.covers_through_created_at = _stamp(index)
        await session.commit()


_BASE = datetime(2021, 1, 1, tzinfo=UTC).replace(tzinfo=None)


def _stamp(index: int) -> datetime:
    """This turn's ``created_at`` per :data:`_TIMELINE`.

    Set explicitly rather than left to SQLite's server default, which would land
    every row in one second and make the ``(created_at, id)`` total order the
    cursor relies on diverge from insertion order.
    """
    return _BASE + timedelta(seconds=_TIMELINE[index][0])


def _mid(index: int) -> uuid.UUID:
    """This turn's message id per :data:`_TIMELINE` — DETERMINISTIC, deliberately.

    Real ids are UUID4, so id order and time order are independent; a fixture
    that leans on random ids to produce that independence reproduces the
    interesting orderings only sometimes. Fixing them makes the disagreement in
    the 6-8 cluster a property of the fixture rather than of the seed.
    """
    return uuid.UUID(int=_TIMELINE[index][1])


@pytest_asyncio.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Acme")
            users = UserRepository(seed, tenant.id)
            owner = await users.create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            stranger = await users.create(
                email="mallory@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            chat = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=owner.id, model="m", title="t"
            )
            collection = await CollectionRepository(seed, tenant.id).create(
                owner_id=owner.id, name="c"
            )
            document = await DocumentRepository(seed, tenant.id).create(
                owner_id=owner.id,
                collection_id=collection.id,
                filename="q3-plan.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                storage_key=f"{tenant.id}/q3-plan.pdf",
                acl_enforced=False,
            )
            chunks = await ChunkRepository(seed, tenant.id).replace_for_document(
                document.id, [ChunkInput(text="margin was 41%", char_start=0, char_end=14)]
            )
            chunk = chunks[0]

            messages = MessageRepository(seed, tenant.id)
            ids: list[uuid.UUID] = []
            for i in range(_TURNS):
                role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
                row = await messages.add_with_id(
                    message_id=_mid(i),
                    session_id=chat.id,
                    role=role,
                    content=f"turn {i}: the sky is chat_service",
                )
                ids.append(row.id)
            for i, mid in enumerate(ids):
                await seed.execute(
                    update(models.Message)
                    .where(models.Message.id == mid)
                    .values(created_at=_stamp(i))
                )
            # Turn 3 (assistant, inside the compacted range) cited the document.
            await CitationRepository(seed, tenant.id).add(
                message_id=ids[3], chunk_id=chunk.id, char_start=0, char_end=14, score=0.9
            )

            # --- the two scopes the recall query must respect --------------------
            # Neither is exercised by the conversation above, and BOTH are one
            # deleted WHERE clause away from a cross-user or cross-tenant read.
            # A single-session, single-tenant fixture cannot tell the difference,
            # so the decoys exist to make those clauses load-bearing.
            #
            # (a) ANOTHER session, SAME owner + tenant — only `session_id` keeps
            #     its turns out. Same owner deliberately: the ownership predicate
            #     passes, so if this leaks it is the query's fault alone.
            other_chat = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=owner.id, model="m", title="other"
            )
            await messages.add_with_id(
                message_id=uuid.UUID(int=500),
                session_id=other_chat.id,
                role=MessageRole.USER,
                content="turn 0: DECOY from another session of mine",
            )
            # (b) ANOTHER tenant, on the SAME session id. Not a shape the app can
            #     write — but it is precisely what the `tenant_id` clause defends
            #     against, and without it the clause is decorative: with the row
            #     in its own session, `session_id` alone already excludes it, so
            #     deleting `tenant_id` from the query passes every test. RLS is
            #     not armed on the offline SQLite either, which leaves the
            #     repository predicate as the whole of INV-1 here.
            other_tenant = await TenantRepository(seed).create(name="Globex")
            await MessageRepository(seed, other_tenant.id).add_with_id(
                message_id=uuid.UUID(int=600),
                session_id=chat.id,
                role=MessageRole.USER,
                content="turn 0: DECOY from another tenant",
            )
            for decoy in (uuid.UUID(int=500), uuid.UUID(int=600)):
                await seed.execute(
                    update(models.Message)
                    .where(models.Message.id == decoy)
                    .values(created_at=_stamp(0))
                )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                tenant_id=tenant.id,
                owner_id=owner.id,
                stranger_id=stranger.id,
                session_id=chat.id,
                message_ids=ids,
                document_id=document.id,
                chunk_id=chunk.id,
            )
    finally:
        await engine.dispose()


# --- the bound ---------------------------------------------------------------


async def test_the_compacted_range_and_the_live_window_partition_the_session(ctx: _Ctx) -> None:
    """Every message is in exactly one of the two sets, for every cursor.

    This is the property the whole feature rests on, and it is a property of two
    predicates written in different files: ``list_for_session_after`` builds the
    prompt's live window, ``search_for_session_before`` builds recall's range.
    Either can drift — the tolerance window, the tie rule, the inequality
    direction — and drift is invisible in isolation. A GAP means turns no path
    can ever reach; an OVERLAP means recall spends the model's budget replaying
    text already sitting in its context.

    Asserted across every cursor position rather than one, because the tie
    branch (same-second peers) only engages at particular boundaries.
    """
    async with ctx.sessionmaker() as session:
        repo = MessageRepository(session, ctx.tenant_id)
        everything = {m.id for m in await repo.list_for_session(ctx.session_id)}
        assert len(everything) == _TURNS

        for index in range(_TURNS):
            live = await repo.list_for_session_after(
                ctx.session_id,
                after_created_at=_stamp(index),
                after_message_id=ctx.message_ids[index],
            )
            compacted = await repo.search_for_session_before(
                ctx.session_id,
                before_created_at=_stamp(index),
                before_message_id=ctx.message_ids[index],
                limit=_TURNS * 2,
            )
            live_ids = {m.id for m in live}
            compacted_ids = {m.id for m in compacted}
            assert not (live_ids & compacted_ids), f"overlap at cursor {index}"
            assert live_ids | compacted_ids == everything, f"gap at cursor {index}"


async def test_recall_reaches_only_turns_that_left_the_prompt(ctx: _Ctx) -> None:
    """The no-query read returns the newest COMPACTED turns, never live ones."""
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary="we discussed the sky")
        outcome = await ctx.reader(session).recall(query=None, limit=3)

    assert outcome.compaction_started
    # Newest three of the compacted range (4, 5, 6), rendered oldest → newest.
    assert [t.content.split(":")[0] for t in outcome.turns] == ["turn 4", "turn 5", "turn 6"]
    # And nothing from the live window (7..11), which is verbatim in the prompt.
    assert all("turn 7" not in t.content for t in outcome.turns)


# --- ownership ---------------------------------------------------------------


async def test_a_same_tenant_stranger_recalls_nothing(ctx: _Ctx) -> None:
    """RLS is tenant-only, so this predicate is the ONLY thing standing here.

    ``chat_sessions``/``messages`` policies are keyed on ``tenant_id`` alone
    (migration 0007), and a tool handler does not route through ``ChatService``,
    where ownership normally lives. A colleague in the same tenant is therefore
    stopped by nothing but the seam's own check — so the seam has one.
    """
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary="we discussed the sky")
        outcome = await ctx.reader(session, user_id=ctx.stranger_id).recall(query=None, limit=5)

    assert outcome.turns == ()
    # Existence non-disclosure (INV-2): the stranger learns nothing about whether
    # the session exists — the shape is identical to "no matches".
    assert outcome.refusal is None


# --- lockstep with the prompt builder ----------------------------------------


async def test_a_cursor_with_no_summary_text_compacts_nothing(ctx: _Ctx) -> None:
    """``send_message`` sends the FULL history unless the summary TEXT is set.

    A row can carry a coverage cursor with no text (the evidence-only upsert
    path). The prompt builder ignores such a row and sends everything, so recall
    must find nothing — otherwise the model burns budget re-reading turns already
    in front of it. Two predicates, one meaning; asserted rather than assumed.
    """
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary=None)
        outcome = await ctx.reader(session).recall(query=None, limit=5)

    assert outcome.compaction_started is False
    assert outcome.turns == ()


async def test_recall_is_empty_before_the_summariser_has_ever_run(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        outcome = await ctx.reader(session).recall(query=None, limit=5)
    assert outcome.compaction_started is False


# --- search ------------------------------------------------------------------


async def test_like_metacharacters_in_a_term_are_literal(ctx: _Ctx) -> None:
    """``_`` is a LIKE wildcard AND an ordinary character in real conversation.

    Unescaped, a search for ``chat_service`` matches ``chatXservice`` — a WRONG
    recall, not a broad one: the model would quote back words the user never
    said. The test therefore needs a turn that ONLY the wildcard reading matches,
    seeded here. An earlier version searched for ``chatXservice`` against a
    corpus containing no such string, so it returned nothing whether or not
    ``_`` was escaped — verified: a mutant escaping ``\\`` and ``%`` but not
    ``_`` passed all 32 tests.
    """
    async with ctx.sessionmaker() as session:
        # Matches "chat_service" ONLY if `_` is treated as a wildcard.
        await MessageRepository(session, ctx.tenant_id).add_with_id(
            message_id=uuid.UUID(int=700),
            session_id=ctx.session_id,
            role=MessageRole.USER,
            content="we deployed chatXservice today",
        )
        await session.execute(
            update(models.Message)
            .where(models.Message.id == uuid.UUID(int=700))
            .values(created_at=_stamp(0))
        )
        await ctx.set_cursor(session, index=_CURSOR, summary="s")
        reader = ctx.reader(session, max_calls=4)
        literal = await reader.recall(query="chat_service", limit=20)
        wildcard = await reader.recall(query="chat%service", limit=20)

    matched = [t.content for t in literal.turns]
    assert matched, "the literal term should still match the seeded turns"
    assert not any("chatXservice" in c for c in matched), (
        "'_' matched an arbitrary character — a search for chat_service returned a "
        "turn that says chatXservice"
    )
    assert wildcard.turns == (), "'%' must not match any character"


async def test_terms_are_anded(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary="s")
        reader = ctx.reader(session, max_calls=4)
        both = await reader.recall(query="turn 4", limit=10)
        one_absent = await reader.recall(query="turn helicopter", limit=10)

    assert [t.content.split(":")[0] for t in both.turns] == ["turn 4"]
    assert one_absent.turns == ()


async def test_an_overlong_query_cannot_build_an_unbounded_predicate(ctx: _Ctx) -> None:
    """The seam clamps terms itself, not only behind today's handler."""
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary="s")
        # Far more terms than the bound, each far longer than the bound.
        query = " ".join(["turn" + "x" * 500] * 50)
        outcome = await ctx.reader(session).recall(query=query, limit=5)
    # Bounded and answered rather than expanded into a 50-clause LIKE chain.
    assert outcome.turns == ()


async def test_recall_never_crosses_into_another_session_or_tenant(ctx: _Ctx) -> None:
    """The two WHERE clauses a single-session fixture cannot make load-bearing.

    ``search_for_session_before`` filters on tenant AND session. Neither is
    covered by the owner predicate: the decoy session has the SAME owner, so
    ownership passes and only ``session_id`` keeps it out; and RLS is not armed
    on the offline SQLite, so ``tenant_id`` here is the whole of INV-1. Delete
    either clause and this is a cross-conversation — or cross-tenant — read.
    """
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary="s")
        # ``DECOY`` appears only in the other session and the other tenant.
        found = await ctx.reader(session, max_calls=4).recall(query="DECOY", limit=20)
        # And the unfiltered read must not sweep them in either.
        everything = await ctx.reader(session, max_calls=4).recall(query=None, limit=50)

    assert found.turns == ()
    assert all("DECOY" not in t.content for t in everything.turns)


async def test_system_turns_are_not_recallable(ctx: _Ctx) -> None:
    """A persisted ``system`` row is prompt scaffolding, not conversation."""
    async with ctx.sessionmaker() as session:
        row = await MessageRepository(session, ctx.tenant_id).add(
            session_id=ctx.session_id, role=MessageRole.SYSTEM, content="turn 0: SCAFFOLD"
        )
        await session.execute(
            update(models.Message).where(models.Message.id == row.id).values(created_at=_stamp(0))
        )
        await ctx.set_cursor(session, index=_CURSOR, summary="s")
        outcome = await ctx.reader(session).recall(query="SCAFFOLD", limit=5)

    assert outcome.turns == ()


# --- evidence is ids, never text ---------------------------------------------


async def test_a_recalled_turn_reports_its_cited_documents_as_ids(ctx: _Ctx) -> None:
    """The seam hands back ids so the HANDLER can re-check them (design rule 1)."""
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary="s")
        outcome = await ctx.reader(session).recall(query="turn 3", limit=5)

    assert len(outcome.turns) == 1
    assert outcome.turns[0].cited_document_ids == (ctx.document_id,)


async def test_the_citation_lookup_never_loads_passage_text(ctx: _Ctx) -> None:
    """The redaction is a property of the QUERY, not of the code downstream.

    ``list_for_messages_hydrated_batch`` joins ``chunks`` for the snippet; recall
    uses an ids-only query instead. If it ever went back to the hydrated one, the
    prose it must not emit would be sitting in memory one forgotten field away
    from the model — so the shape is asserted, not just the behaviour.
    """
    async with ctx.sessionmaker() as session:
        pairs = await CitationRepository(session, ctx.tenant_id).document_ids_for_messages(
            [ctx.message_ids[3]]
        )
    assert pairs == {ctx.message_ids[3]: {ctx.document_id}}
    # Nothing in the returned structure can carry chunk text: it is ids only.
    assert all(isinstance(v, set) for v in pairs.values())


# --- the stopping rule -------------------------------------------------------


async def test_the_per_answer_call_budget_refuses_rather_than_loops(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary="s")
        reader = ctx.reader(session, max_calls=2)
        assert (await reader.recall(query="turn 1", limit=3)).refusal is None
        assert (await reader.recall(query="turn 2", limit=3)).refusal is None
        spent = await reader.recall(query="turn 3", limit=3)

    assert spent.refusal is not None
    assert spent.turns == ()


async def test_an_identical_repeat_does_not_burn_a_call(ctx: _Ctx) -> None:
    """A repeat is refused BEFORE the budget, so a bad query is not fatal.

    Charging the repeat would mean one duplicated call costs the model its only
    remaining chance at a better search — turning the stopping rule into a
    penalty for the exact mistake it exists to interrupt.
    """
    async with ctx.sessionmaker() as session:
        await ctx.set_cursor(session, index=_CURSOR, summary="s")
        reader = ctx.reader(session, max_calls=2)
        await reader.recall(query="turn 1", limit=3)
        repeat = await reader.recall(query="TURN   1", limit=3)  # normalised: same query
        assert repeat.refusal is not None
        # The second genuine search still runs.
        assert (await reader.recall(query="turn 2", limit=3)).refusal is None


# --- the handler: permission re-check ----------------------------------------


class _FakeRetrieval:
    """Only the one method the handler uses; ``permits`` is the current grant."""

    def __init__(self, permits: dict[uuid.UUID, str]) -> None:
        self.permits = permits
        self.asked: list[list[uuid.UUID]] = []

    async def permitted_document_names(
        self, *, principal: object, document_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        self.asked.append(list(document_ids))
        return {d: n for d, n in self.permits.items() if d in document_ids}


class _FakeTranscript:
    def __init__(self, outcome: RecallOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str | None, int]] = []

    async def recall(self, *, query: str | None, limit: int) -> RecallOutcome:
        self.calls.append((query, limit))
        return self.outcome


def _ctx_for(
    outcome: RecallOutcome,
    *,
    permits: dict[uuid.UUID, str] | None = None,
    snippet_budget: int = 600,
    max_k: int = 50,
) -> tuple[ToolContext, _FakeRetrieval]:
    retrieval = _FakeRetrieval(permits or {})
    context = ToolContext(
        principal=Principal(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=(Role.MEMBER,)),
        retrieval=retrieval,  # type: ignore[arg-type]
        transcript=_FakeTranscript(outcome),
        snippet_budget=snippet_budget,
        max_k=max_k,
    )
    return context, retrieval


def _turn(
    content: str, *, cited: tuple[uuid.UUID, ...] = (), role: str = "assistant"
) -> RecalledTurn:
    return RecalledTurn(
        role=role,
        created_at=datetime(2021, 1, 1, 0, 5, tzinfo=UTC),
        content=content,
        cited_document_ids=cited,
    )


async def test_a_turn_citing_a_revoked_document_is_withheld_whole() -> None:
    """The #536 leak class, reached from a third direction.

    The turn is prose the model wrote while reading a document. Replaying it
    verbatim re-serves that document's content to someone whose grant was pulled
    — "the user saw it last turn" proves nothing about now. It is withheld WHOLE
    rather than trimmed because the quotation can be anywhere in the prose,
    paraphrased, or split across sentences; there is no substring to excise.
    """
    doc = uuid.uuid4()
    outcome = RecallOutcome(turns=(_turn("Q3 margin was 41%, per the plan.", cited=(doc,)),))

    revoked_ctx, _ = _ctx_for(outcome, permits={})
    result = await _read_conversation({}, revoked_ctx)
    assert "41%" not in result.content
    assert "withheld" in result.content
    assert result.payload["withheld_turns"] == 1

    # The control: while the grant stands, the SAME turn comes back in full.
    granted_ctx, _ = _ctx_for(outcome, permits={doc: "q3-plan.pdf"})
    allowed = await _read_conversation({}, granted_ctx)
    assert "41%" in allowed.content
    assert allowed.payload["withheld_turns"] == 0


async def test_a_partially_revoked_turn_is_withheld_not_partly_shown() -> None:
    """One revoked citation is enough. The turn cites two documents; a rule that
    checked "any permitted" instead of "all permitted" would serve the prose that
    quotes the revoked one."""
    kept, pulled = uuid.uuid4(), uuid.uuid4()
    outcome = RecallOutcome(turns=(_turn("both figures: 41% and 12%", cited=(kept, pulled)),))
    context, _ = _ctx_for(outcome, permits={kept: "kept.pdf"})
    result = await _read_conversation({}, context)
    assert "41%" not in result.content
    assert result.payload["withheld_turns"] == 1


async def test_the_users_own_words_are_never_withheld() -> None:
    """A user turn is what they typed. Redacting it would be theatre — they can
    read it in the UI right now — and would make recall useless for the case it
    exists for ("what did I ask you earlier?")."""
    outcome = RecallOutcome(turns=(_turn("what was the Q3 margin?", role="user"),))
    context, retrieval = _ctx_for(outcome, permits={})
    result = await _read_conversation({}, context)
    assert "what was the Q3 margin?" in result.content
    # No citations to check ⇒ no permission query at all.
    assert retrieval.asked == []


async def test_surviving_evidence_is_reported_as_ids_and_CURRENT_names() -> None:
    """Names come from the permission query, never from the stored turn.

    A document renamed since the turn was written must surface under its name
    now; a stored name would be stale, and a stale name is the one thing the
    model would confidently repeat.
    """
    doc = uuid.uuid4()
    outcome = RecallOutcome(turns=(_turn("as noted in the plan", cited=(doc,)),))
    context, _ = _ctx_for(outcome, permits={doc: "renamed-q3.pdf"})
    result = await _read_conversation({}, context)
    assert "renamed-q3.pdf" in result.content
    assert str(doc) in result.content


# --- the handler: it must not undo #491 --------------------------------------


async def test_a_recall_result_carries_no_passages_and_no_document_ids() -> None:
    """Two consequences, both deliberate.

    No ``passages`` ⇒ the result never enters the compactor's ``cited_snippets``
    map, so it lands in tier 1 and is the FIRST thing digested under context
    pressure. Recall is navigation; evidence must outrank it.

    No ``document_ids`` ⇒ recall contributes no citation. There is no
    ``conversation`` citation kind and this does not invent one:
    ``citations.chunk_id`` is a NOT NULL FK to ``chunks``, and INV-3 says a
    citation resolves to a permitted passage the model actually read. The model
    re-retrieves in order to cite.
    """
    doc = uuid.uuid4()
    outcome = RecallOutcome(turns=(_turn("as noted", cited=(doc,)),))
    context, _ = _ctx_for(outcome, permits={doc: "q3.pdf"})
    result = await _read_conversation({}, context)
    assert result.passages == ()
    assert result.document_ids == ()


async def test_recall_results_are_shed_before_retrieval_results() -> None:
    """The tiering asserted end-to-end, not merely by the absent field.

    A run under context pressure holds both kinds of tool result. If recall ever
    started carrying passages, it would join the CITED tier and compete with the
    evidence the answer is grounded in — #491's degrade order inverted, silently
    and only under pressure, which is exactly when nobody is looking.

    The ``cited_snippets`` map is DERIVED from the handler's own result the way
    the runtime derives it (a result contributes an entry only if it carries
    passages), not hand-written — otherwise this would test the compactor while
    quietly assuming the very thing it claims to prove.
    """
    doc = uuid.uuid4()
    handler_result = await _read_conversation(
        {},
        _ctx_for(RecallOutcome(turns=(_turn("as noted", cited=(doc,)),)), permits={doc: "q.pdf"})[
            0
        ],
    )
    bulky = "evidence " * 4000
    recalled = "earlier turn " * 4000
    messages = [
        ChatMessage(role=LlmRole.SYSTEM, content="sys"),
        ChatMessage(role=LlmRole.USER, content="the question"),
        ChatMessage(role=LlmRole.ASSISTANT, content="", tool_calls=()),
        ChatMessage(role=LlmRole.TOOL, content=recalled, tool_call_id="call_recall"),
        ChatMessage(role=LlmRole.TOOL, content=bulky, tool_call_id="call_search"),
        ChatMessage(role=LlmRole.ASSISTANT, content="", tool_calls=()),
    ]
    cited_snippets: dict[str, tuple[str, ...]] = {"call_search": ("evidence evidence",)}
    if handler_result.passages:
        cited_snippets["call_recall"] = tuple(p.text for p in handler_result.passages)

    # Counting in characters (``counter=len``) so the budget is legible: the two
    # results total ~88k chars, and this budget sits comfortably above what
    # remains once the UNCITED one is digested — so tier 1 alone suffices and
    # tier 2 must never engage.
    fitted = fit_transcript(
        messages,
        model="gpt-4o-mini",
        config=ContextConfig(output_headroom_tokens=0),
        counter=len,
        max_input_resolver=lambda _m: 60_000,
        cited_snippets=cited_snippets,
    )
    by_call = {m.tool_call_id: m.content for m in fitted if m.tool_call_id}
    assert len(by_call["call_recall"]) < len(recalled), "recall should compact first"
    assert by_call["call_search"] == bulky, "cited evidence must survive while recall can shrink"


async def test_output_is_bounded_by_the_run_s_snippet_budget() -> None:
    """A tight window lowers the budget, and recall respects it like every other
    tool — a recall issued after assembly cannot overflow the context it was
    budgeted against."""
    long_turn = "x" * 5_000
    outcome = RecallOutcome(turns=tuple(_turn(long_turn) for _ in range(6)))
    context, _ = _ctx_for(outcome, snippet_budget=120)
    result = await _read_conversation({}, context)
    # Six turns, each clipped to the budget, plus framing — nowhere near 30_000.
    assert result.content.count("…") == 6
    assert len(result.content) < 6 * 400


async def test_k_is_clamped_to_the_budget_derived_ceiling() -> None:
    context, _ = _ctx_for(RecallOutcome(turns=()), max_k=2)
    fake = context.transcript
    assert isinstance(fake, _FakeTranscript)
    await _read_conversation({"k": 99}, context)
    assert fake.calls[-1][1] == 2


# --- the handler: refusals the run survives ----------------------------------


async def test_no_transcript_seam_is_a_typed_refusal_not_a_crash() -> None:
    """A sub-agent / headless run has no conversation. Same shape as
    ``run_python`` without its sandbox: ``ok=False``, the run continues."""
    context = ToolContext(
        principal=Principal(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=(Role.MEMBER,)),
        retrieval=_FakeRetrieval({}),  # type: ignore[arg-type]
    )
    result = await _read_conversation({}, context)
    assert result.ok is False
    assert "unavailable" in result.content


async def test_nothing_compacted_says_so_instead_of_returning_empty() -> None:
    """An empty list reads as 'search harder'; the structural stopping rule has
    to be a sentence, or the model retries against a range that cannot grow."""
    context, _ = _ctx_for(RecallOutcome(compaction_started=False))
    result = await _read_conversation({}, context)
    assert result.ok is True
    assert "Nothing has been compacted" in result.content


async def test_a_seam_refusal_surfaces_as_a_wall() -> None:
    context, _ = _ctx_for(RecallOutcome(refusal="stop asking"))
    result = await _read_conversation({}, context)
    assert result.ok is False
    assert result.content == "stop asking"


async def test_a_non_string_query_is_rejected_without_reaching_the_seam() -> None:
    context, _ = _ctx_for(RecallOutcome(turns=()))
    fake = context.transcript
    assert isinstance(fake, _FakeTranscript)
    result = await _read_conversation({"query": {"not": "a string"}}, context)
    assert result.ok is False
    assert fake.calls == []


async def test_too_many_terms_is_rejected_with_a_reason_the_model_can_act_on() -> None:
    context, _ = _ctx_for(RecallOutcome(turns=()))
    result = await _read_conversation(
        {"query": " ".join("w" * 3 for _ in range(_MAX_TERMS + 1))}, context
    )
    assert result.ok is False
    assert "fewer, sharper words" in result.content


# --- governance --------------------------------------------------------------


def test_read_conversation_is_a_t0_read_only_default_tool() -> None:
    definition = get_tool(READ_CONVERSATION_TOOL_NAME)
    assert definition.read_only is True
    assert definition.requires_approval is False
    assert READ_CONVERSATION_TOOL_NAME in default_allowlist()


def test_the_schema_has_no_session_argument() -> None:
    """The seam is bound to one session at construction; the model must have no
    way to name another. A session_id argument would move the ownership decision
    from the runtime into model output."""
    schema = get_tool(READ_CONVERSATION_TOOL_NAME).json_schema
    assert set(schema["properties"]) == {"query", "k"}
    assert "required" not in schema


def test_the_runtime_wires_the_seam_only_when_the_tool_is_allowed() -> None:
    """Deny-by-default: a session that cannot recall carries no recall plumbing."""
    from app.services.chat_runtime import ChatRuntime

    runtime = ChatRuntime.__new__(ChatRuntime)
    runtime._principal = Principal(  # noqa: SLF001 — the wiring seam
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=(Role.MEMBER,)
    )
    assert (
        runtime._build_transcript_seam(  # noqa: SLF001
            session=None,  # type: ignore[arg-type]  — never touched when denied
            allowed=frozenset({"search_text"}),
            session_id=uuid.uuid4(),
        )
        is None
    )
    assert (
        runtime._build_transcript_seam(  # noqa: SLF001
            session=None,  # type: ignore[arg-type]  — repositories are lazy here
            allowed=frozenset({READ_CONVERSATION_TOOL_NAME}),
            session_id=uuid.uuid4(),
        )
        is not None
    )


# --- concurrency: the seam must not ride the runtime session (#412) ----------


async def test_for_session_rebinds_the_database_but_shares_the_budget(ctx: _Ctx) -> None:
    """The two halves of the fan-out fix, asserted separately.

    Rebinding without sharing would make concurrency a way to BUY recall calls:
    two recalls in one batch, each with a fresh allowance of two. Sharing without
    rebinding would race the runtime's ``AsyncSession`` across coroutines. Both
    are one-line mistakes, so both are pinned.
    """
    async with ctx.sessionmaker() as runtime_session, ctx.sessionmaker() as call_session:
        await ctx.set_cursor(runtime_session, index=_CURSOR, summary="s")
        parent = ctx.reader(runtime_session, max_calls=2)
        view = parent.for_session(call_session)

        # Different session…
        assert view._messages._session is call_session  # noqa: SLF001
        assert parent._messages._session is runtime_session  # noqa: SLF001
        # …one budget.
        assert view._budget is parent._budget  # noqa: SLF001

        assert (await parent.recall(query="turn 1", limit=3)).refusal is None
        assert (await view.recall(query="turn 2", limit=3)).refusal is None
        # The view spent the parent's second call, so the answer is out of them.
        assert (await parent.recall(query="turn 3", limit=3)).refusal is not None


async def test_a_fanned_out_recall_gets_an_isolated_session(ctx: _Ctx) -> None:
    """Driven through the REAL scheduler, because that is where the bug was.

    ``_run_tool_batch`` re-scopes ``retrieval`` and strips ``artifacts``/
    ``sandbox`` for every fanned-out call — every collaborator that closes over
    the runtime session. A transcript seam is the first READ seam that is not
    ``retrieval``, so it was silently left holding the runtime session and would
    have used it from a coroutine overlapping every other call in the batch: the
    exact race that keeps MCP tools out of the fan-out entirely.
    """
    from app.domain.llm import ToolCall
    from app.domain.tools import ToolResult
    from app.services.chat_runtime import ChatRuntime

    async with ctx.sessionmaker() as runtime_session:
        runtime = ChatRuntime.__new__(ChatRuntime)
        runtime._principal = ctx.principal()  # noqa: SLF001
        runtime._tool_concurrency = 4  # noqa: SLF001
        runtime._sessionmaker = ctx.sessionmaker  # noqa: SLF001
        runtime._retrieval_factory = lambda _s: object()  # noqa: SLF001

        async def _noop(*_args: object, **_kwargs: object) -> None:
            return None

        runtime._publish_tool_call = _noop  # type: ignore[method-assign]  # noqa: SLF001
        runtime._publish_tool_result = _noop  # type: ignore[method-assign]  # noqa: SLF001

        seam = ctx.reader(runtime_session)
        context = ToolContext(
            principal=ctx.principal(),
            retrieval=object(),  # type: ignore[arg-type]
            transcript=seam,
        )
        scoped: list[ToolContext] = []

        class _Runner:
            """Stands in for ``ToolRunner``, entering the scope exactly as it does."""

            def is_concurrency_safe(self, name: str) -> bool:
                return True

            def requires_call_scope(self, name: str) -> bool:
                return True

            def claim_ordinal(self) -> int:
                return 0

            async def run(
                self,
                *,
                call: ToolCall,
                context: ToolContext,
                message_id: uuid.UUID | None = None,
                ordinal: int | None = None,
                scope: object = None,
            ) -> ToolResult:
                assert scope is not None
                async with scope() as inner:  # type: ignore[operator]
                    scoped.append(inner)
                return ToolResult(call_id=call.id, name=call.name, ok=True, content="")

        # Two recalls in ONE batch — the exact shape that would buy a fresh
        # allowance per call if the budget rode the per-call view.
        calls = [
            ToolCall(id="a", name=READ_CONVERSATION_TOOL_NAME, arguments={}),
            ToolCall(id="b", name=READ_CONVERSATION_TOOL_NAME, arguments={"query": "sky"}),
        ]
        await runtime._run_tool_batch(  # noqa: SLF001
            state=None,  # type: ignore[arg-type]  — publishes are stubbed out
            runner=_Runner(),  # type: ignore[arg-type]
            audit=None,  # type: ignore[arg-type]  — neither call is a retrieval audit
            context=context,
            calls=calls,
            message_id=uuid.uuid4(),
        )

    assert len(scoped) == 2
    for inner in scoped:
        assert inner.transcript is not None
        # Not the runtime seam, and not the runtime's session.
        assert inner.transcript is not seam
        assert inner.transcript._messages._session is not runtime_session  # noqa: SLF001
        # Still one budget across the whole batch.
        assert inner.transcript._budget is seam._budget  # noqa: SLF001
    # Each concurrent call got its OWN session, not a shared one.
    first, second = scoped
    assert (
        first.transcript._messages._session  # noqa: SLF001
        is not second.transcript._messages._session  # noqa: SLF001
    )


def test_recalled_turn_carries_no_snippet_field() -> None:
    """A structural guard on the leak surface.

    ``RecalledTurn`` is what the seam hands the handler. If it ever grew a
    snippet/passage-text field, redacting the turn's ``content`` would stop being
    sufficient and the withholding test above would keep passing while prose
    escaped through the new field.
    """
    fields = set(RecalledTurn.__dataclass_fields__)
    assert fields == {"role", "created_at", "content", "cited_document_ids"}
    # And it is frozen, so nothing downstream can un-redact one in place.
    with pytest.raises(FrozenInstanceError):
        _turn("x").content = "y"  # type: ignore[misc]
