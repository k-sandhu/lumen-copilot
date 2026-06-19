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
    carrying ``tool_calls`` / ``finish_reason``). Consecutive ``stream_tools``
    calls pop successive turns, so a tool-call turn followed by a final-answer
    turn drives the loop exactly once.
    """

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self.calls = 0

    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None
    ) -> AsyncIterator[StreamEvent]:
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        for ev in turn:
            yield ev


class _BoomGateway:
    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None
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
    ctx: _Ctx, *, gateway: object, retrieval: object, backplane: InMemoryBackplane
) -> ChatRuntime:
    return ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        principal=ctx.principal,
        request_id="req-1",
        source_ip="127.0.0.1",
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
