"""Chat runtime tests — the agentic grounded answer loop (CC-6 #24 / CC-11 #26).

Drives :class:`~app.services.chat_runtime.ChatRuntime` end-to-end **offline**: a
scripted fake gateway emits the tool-aware stream (text + tool calls), a fake
retrieval returns permitted passages, and the in-memory backplane captures the
published envelopes. The real agentic loop (a live model deciding to search) needs
an OPENROUTER_API_KEY + the running stack — these tests fix the *plumbing*: the
exact WS lifecycle, grounding/citation persistence (INV-3), the honest
no-source path, and the terminal-error contract.

Headlines:
* full lifecycle: start → tool_call → tool_result → citation → delta → done,
  with a single terminal and a monotonic seq;
* citations are persisted to the assistant message and reload via the repo
  (CC-11 AC-2) — and only ever describe retrieved (permitted) passages (INV-3);
* a turn that retrieved nothing yields a zero-citation answer, shown honestly
  (no fabricated reference) — issue #24 AC-3;
* a gateway failure ends the stream with exactly one terminal ``error`` (and no
  vendor error leaks).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    CitationRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.domain.llm import StreamEvent, ToolCall
from app.domain.retrieval import DocumentMatch, DocumentText, RetrievedPassage
from app.realtime.backplane import InMemoryBackplane
from app.services.chat_runtime import ChatRuntime

import app.db.models  # noqa: F401  isort: skip


# --- Fakes ------------------------------------------------------------------


class _ScriptedGateway:
    """A gateway whose ``stream_tools`` replays a scripted list of turns.

    Each turn is a list of ``StreamEvent``s (text chunks then a terminal event
    carrying ``tool_calls`` / ``finish_reason``). Consecutive *tool-choice=auto*
    ``stream_tools`` calls pop successive turns, so a tool-call turn followed by a
    final-answer turn drives the loop exactly once.

    When the runtime exhausts its tool-turn budget it makes one **forced**
    ``stream_tools`` call with ``tool_choice="none"`` to synthesise a tool-free
    answer (issue #148). This fake models a real provider's response to that:
    such a call yields the ``synthesis`` script (a tool-free turn) instead of
    advancing the ``turns`` list — so a script of all-tool turns still ends with
    a clean synthesised answer. ``auto_calls`` / ``synthesis_calls`` are exposed
    so a test can assert exactly how many tool turns ran vs the forced final one.
    """

    def __init__(
        self,
        turns: list[list[StreamEvent]],
        *,
        synthesis: list[StreamEvent] | None = None,
    ) -> None:
        self._turns = turns
        self._synthesis = synthesis
        self.calls = 0
        self.auto_calls = 0
        self.synthesis_calls = 0

    async def stream_tools(
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        if tool_choice == "none":
            self.synthesis_calls += 1
            turn = (
                self._synthesis
                if self._synthesis is not None
                else [StreamEvent(finish_reason="stop")]
            )
        else:
            turn = self._turns[min(self.auto_calls, len(self._turns) - 1)]
            self.auto_calls += 1
        for ev in turn:
            yield ev


class _BoomGateway:
    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None, tool_choice: object = None
    ) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover — unreachable, makes this an async generator


class _FakeRetrieval:
    """A retrieval stand-in returning fixed permitted passages for search_text."""

    def __init__(self, passages: list[RetrievedPassage]) -> None:
        self._passages = passages
        self.queries: list[str] = []

    async def search_text(
        self, *, principal: object, query: str, k: int, collection_ids: object = None
    ) -> list[RetrievedPassage]:
        self.queries.append(query)
        return list(self._passages)

    async def search_documents(
        self, *, principal: object, name_or_query: str, k: int = 10
    ) -> list[DocumentMatch]:
        return []

    async def get_document(self, *, principal: object, document_id: object) -> DocumentText | None:
        return None


def _passage(document_id: uuid.UUID, chunk_id: uuid.UUID, doc_name: str) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=chunk_id,
        document_id=document_id,
        document_name=doc_name,
        ord=0,
        text="The standard deduction for 2024 is $14,600.",
        char_start=100,
        char_end=143,
        score=0.91,
    )


# --- Fixture: SQLite + a seeded tenant/user/session/document/chunk ----------


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        principal: Principal,
        session_id: uuid.UUID,
        document_id: uuid.UUID,
        chunk_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.principal = principal
        self.session_id = session_id
        self.document_id = document_id
        self.chunk_id = chunk_id
        self.tenant_id = tenant_id


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
            user = await UserRepository(seed, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            from app.db.repositories import (
                ChunkInput,
                ChunkRepository,
                CollectionRepository,
                DocumentRepository,
            )

            coll = await CollectionRepository(seed, tenant.id).create(owner_id=user.id, name="c")
            doc = await DocumentRepository(seed, tenant.id).create(
                owner_id=user.id,
                collection_id=coll.id,
                filename="taxes.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                storage_key=f"{tenant.id}/taxes.pdf",
            )
            chunks = await ChunkRepository(seed, tenant.id).replace_for_document(
                doc.id,
                [
                    ChunkInput(
                        text="The standard deduction for 2024 is $14,600.",
                        char_start=100,
                        char_end=143,
                    )
                ],
            )
            session = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=user.id, model="anthropic/claude-opus-4.8", title="t"
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                principal=Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,)),
                session_id=session.id,
                document_id=doc.id,
                chunk_id=chunks[0].id,
                tenant_id=tenant.id,
            )
    finally:
        await engine.dispose()


def _runtime(
    ctx: _Ctx,
    *,
    gateway: object,
    retrieval: object,
    backplane: InMemoryBackplane,
    default_max_tool_turns: int = 4,
) -> ChatRuntime:
    return ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        principal=ctx.principal,
        request_id="req-1",
        source_ip="127.0.0.1",
        default_max_tool_turns=default_max_tool_turns,
        retrieval_factory=lambda _session: retrieval,  # type: ignore[arg-type,return-value]
    )


async def _drain(backplane: InMemoryBackplane, stream_id: str) -> list[dict[str, object]]:
    return [env async for env in backplane.subscribe(stream_id)]


# --- Full lifecycle: search → ground → cite → answer → done -----------------


async def test_grounded_answer_full_lifecycle(ctx: _Ctx) -> None:
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [
            # Turn 1: the model calls search_text.
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="search_text", arguments={"query": "deduction"}),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            # Turn 2: the model answers using the retrieved passage.
            [
                StreamEvent(text="The 2024 standard deduction "),
                StreamEvent(text="is $14,600."),
                StreamEvent(finish_reason="stop"),
            ],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What is the 2024 standard deduction?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    types = [e["type"] for e in envs]
    assert types[0] == "start"
    assert types[-1] == "done"
    assert types.count("done") == 1 and "error" not in types
    # Monotonic seq.
    seqs = [e["seq"] for e in envs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # The event vocabulary all appeared.
    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "tool_call" in names
    assert "tool_result" in names
    assert "citation" in names
    # Tokens streamed.
    text = "".join(e["data"]["text"] for e in envs if e["type"] == "delta")  # type: ignore[index]
    assert "14,600" in text
    # done summary reports one citation.
    done = envs[-1]
    assert done["data"]["citationCount"] == 1  # type: ignore[index]


async def test_citation_persisted_and_reloadable(ctx: _Ctx) -> None:
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "q"}),),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="Grounded answer."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )

    # The assistant message + its citation persisted; reload via the repos.
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in messages if m.role.value == "assistant"]
        assert len(assistant) == 1
        assert assistant[0].content == "Grounded answer."
        assert assistant[0].model == "anthropic/claude-opus-4.8"
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message_hydrated(
            assistant[0].id
        )
    assert len(citations) == 1
    cit = citations[0]
    # The citation resolves to the *retrieved* (permitted) passage (INV-3).
    assert cit.chunk_id == ctx.chunk_id
    assert cit.document_id == ctx.document_id
    assert cit.document_name == "taxes.pdf"
    # Source-document offsets (deep-link target, CC-11 AC-3).
    assert (cit.char_start, cit.char_end) == (100, 143)


async def test_zero_citation_answer_is_honest(ctx: _Ctx) -> None:
    # The model searched, retrieval found nothing, and the model gives the
    # honest "couldn't find it" answer — a zero-citation answer, shown as such.
    retrieval = _FakeRetrieval([])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "q"}),),
                    finish_reason="tool_calls",
                )
            ],
            [
                StreamEvent(text="I couldn't find anything in your sources that answers that."),
                StreamEvent(finish_reason="stop"),
            ],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # No citation event; done reports zero citations; the answer is the honest one.
    assert "citation" not in [e.get("name") for e in envs if e["type"] == "event"]
    assert envs[-1]["data"]["citationCount"] == 0  # type: ignore[index]
    async with ctx.sessionmaker() as session:
        msgs = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in msgs if m.role.value == "assistant"][0]
        cits = await CitationRepository(session, ctx.tenant_id).list_for_message(assistant.id)
    assert cits == []
    assert "couldn't find" in assistant.content.lower()


async def test_empty_model_answer_falls_back_to_honest_message(ctx: _Ctx) -> None:
    # The model only searched and never produced answer text — the runtime fills
    # in the honest fallback rather than persisting an empty assistant turn.
    retrieval = _FakeRetrieval([])
    gateway = _ScriptedGateway([[StreamEvent(finish_reason="stop")]])
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    async with ctx.sessionmaker() as session:
        msgs = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in msgs if m.role.value == "assistant"][0]
    assert assistant.content.strip() != ""


def _tool_turn(narration: str, call_id: str) -> list[StreamEvent]:
    """A turn that emits a little narration then requests a search (never answers)."""
    return [
        StreamEvent(text=narration),
        StreamEvent(
            tool_calls=(ToolCall(id=call_id, name="search_text", arguments={"query": "q"}),),
            finish_reason="tool_calls",
        ),
    ]


async def test_tool_budget_exhaustion_forces_a_synthesized_answer(ctx: _Ctx) -> None:
    """Issue #148 regression: if every turn calls a tool until the budget is spent,
    the runtime forces one tool-free synthesis so the answer is real, not narration.

    Without the fix the loop exits via the turn cap with ``answer_text`` equal to
    just the inter-tool narration ("I'll search…", "Let me read…") — the live
    2026-06-24 empty-answer bug. The fix makes a final ``tool_choice="none"`` call
    and persists *that* as the answer.
    """
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        # Every scripted turn requests a tool (with leading narration) — the model
        # never volunteers a tool-free answer within the budget.
        [
            _tool_turn("I'll search your documents for the incident. ", "c1"),
            _tool_turn("Let me read the full postmortem. ", "c2"),
        ],
        # The forced tool-free synthesis the runtime demands once the budget is spent.
        synthesis=[
            StreamEvent(text="The root cause was a bad index migration; "),
            StreamEvent(text="action items: add a canary and a rollback runbook."),
            StreamEvent(finish_reason="stop"),
        ],
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx, gateway=gateway, retrieval=retrieval, backplane=backplane, default_max_tool_turns=2
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What was the root cause and the action items?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # The loop spent its 2-turn budget on tool calls, then forced exactly one
    # tool-free synthesis (tool_choice="none").
    assert gateway.auto_calls == 2
    assert gateway.synthesis_calls == 1
    # Terminal is a single done (no error), and it is NOT the empty-answer fallback.
    assert envs[-1]["type"] == "done"
    assert [e["type"] for e in envs].count("done") == 1 and "error" not in [e["type"] for e in envs]

    expected = (
        "The root cause was a bad index migration; "
        "action items: add a canary and a rollback runbook."
    )
    # Regression (PR #150 review): the LIVE stream must equal the synthesized answer
    # too. The inter-tool narration is never emitted as a delta, so the streamed
    # answer and the stored message agree — no live/persisted divergence.
    streamed = "".join(e["data"]["text"] for e in envs if e["type"] == "delta")  # type: ignore[index]
    assert streamed == expected
    assert "I'll search" not in streamed
    assert "Let me read" not in streamed

    async with ctx.sessionmaker() as session:
        msgs = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in msgs if m.role.value == "assistant"][0]
    # The persisted answer is the synthesized answer — not the inter-tool narration.
    assert assistant.content == expected


async def test_per_tenant_override_caps_tool_turns(ctx: _Ctx) -> None:
    """A tenant's ``max_tool_turns`` override bounds the loop, beating the default (#148).

    The default budget is set high (20); the tenant override is 1. If the override
    were ignored the all-tool script would drive the loop 20 times (the fake clamps
    to its last tool turn); respecting it runs exactly **one** tool turn before the
    forced synthesis. ``auto_calls == 1`` is the proof the override won.
    """
    # Set the per-tenant override to 1 before the answer runs.
    async with ctx.sessionmaker() as session:
        await TenantRepository(session).update(ctx.tenant_id, max_tool_turns=1)
        await session.commit()

    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [_tool_turn("Searching… ", "c1")],
        synthesis=[StreamEvent(text="Grounded final answer."), StreamEvent(finish_reason="stop")],
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx, gateway=gateway, retrieval=retrieval, backplane=backplane, default_max_tool_turns=20
    )

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # Exactly one tool turn ran (the override of 1), then the forced synthesis —
    # not the default of 20.
    assert gateway.auto_calls == 1
    assert gateway.synthesis_calls == 1
    # The streamed answer equals the synthesis (narration suppressed) and matches
    # the persisted message — no live/stored divergence (PR #150 review).
    streamed = "".join(e["data"]["text"] for e in envs if e["type"] == "delta")  # type: ignore[index]
    assert streamed == "Grounded final answer."
    assert "Searching" not in streamed
    async with ctx.sessionmaker() as session:
        msgs = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = [m for m in msgs if m.role.value == "assistant"][0]
    assert assistant.content == "Grounded final answer."


async def test_gateway_failure_ends_with_single_terminal_error(ctx: _Ctx) -> None:
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx, gateway=_BoomGateway(), retrieval=_FakeRetrieval([]), backplane=backplane
    )

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    assert envs[0]["type"] == "start"
    assert envs[-1]["type"] == "error"
    # Exactly one terminal; the vendor message never leaks into the problem.
    assert [e["type"] for e in envs].count("error") == 1
    assert "done" not in [e["type"] for e in envs]
    problem = envs[-1]["problem"]  # type: ignore[index]
    assert problem["status"] == 500
    assert "exploded" not in str(problem)


async def test_retrieval_and_answer_audit_events_emitted(ctx: _Ctx) -> None:
    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "q"}),),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="A."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    from app.db.repositories import AuditEventRepository

    async with ctx.sessionmaker() as session:
        events = await AuditEventRepository(session, ctx.tenant_id).list_recent()
    actions = {e.action for e in events}
    assert "retrieval.query" in actions
    assert "answer.generated" in actions
    answer_ev = next(e for e in events if e.action == "answer.generated")
    assert answer_ev.metadata["citation_count"] == 1
