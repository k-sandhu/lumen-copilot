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

import pytest
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
from app.domain.entities import CodeRunStatus, Role
from app.domain.llm import Completion, StreamEvent, TokenUsage, ToolCall
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
        chat_completion: Completion | None = None,
    ) -> None:
        self._turns = turns
        self._synthesis = synthesis
        # The non-streamed ``chat()`` script (the spec 0006 suggestions call).
        # ``None`` (the default) makes ``chat()`` raise — modelling "no
        # completion available", which the runtime must swallow silently.
        self._chat_completion = chat_completion
        self.calls = 0
        self.auto_calls = 0
        self.synthesis_calls = 0
        self.chat_calls = 0

    async def chat(
        self,
        messages: object,
        *,
        model: object = None,
        api_key: object = None,
        api_base: object = None,
        max_tokens: object = None,
    ) -> Completion:
        self.chat_calls += 1
        if self._chat_completion is None:
            raise RuntimeError("no scripted chat completion")
        return self._chat_completion

    async def stream_tools(
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
        api_key: object = None,
        api_base: object = None,
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
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
        api_key: object = None,
        api_base: object = None,
    ) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover — unreachable, makes this an async generator


class _FakeRetrieval:
    """A retrieval stand-in returning fixed permitted passages for search_text."""

    def __init__(self, passages: list[RetrievedPassage]) -> None:
        self._passages = passages
        self.queries: list[str] = []

    async def search_text(
        self,
        *,
        principal: object,
        query: str,
        k: int,
        collection_ids: object = None,
        document_ids: object = None,
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
    context_config: object = None,
    interactive: bool = True,
    suggestions_enabled: bool = False,
) -> ChatRuntime:
    return ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        principal=ctx.principal,
        request_id="req-1",
        source_ip="127.0.0.1",
        default_max_tool_turns=default_max_tool_turns,
        context_config=context_config,  # type: ignore[arg-type]
        retrieval_factory=lambda _session: retrieval,  # type: ignore[arg-type,return-value]
        interactive=interactive,
        suggestions_enabled=suggestions_enabled,
        suggestions_timeout_seconds=2.0,
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
    # The distinct cited document id is recorded on the answer event (#249) so
    # the audit read-path can synthesise an allow-candidate and the "Answers
    # cited" KPI counts this grounded answer as cited.
    assert answer_ev.metadata["document_ids"] == [str(ctx.document_id)]


# ---------------------------------------------------------------------------
# run_python seam wiring (issue #231) — deny-by-default + the streamed events.
# ---------------------------------------------------------------------------


class _RecordingSandboxFactory:
    """A sandbox factory that records whether it was invoked + returns a fake seam.

    Lets the runtime wiring tests assert deny-by-default: the factory is called ONLY
    when a run's allow-list offers ``run_python`` (below), never for an ad-hoc
    session on the default allow-list.
    """

    def __init__(self, seam: object) -> None:
        self._seam = seam
        self.contexts: list[object] = []

    def __call__(self, sandbox_ctx: object) -> object:
        self.contexts.append(sandbox_ctx)
        return self._seam


class _FakeSeam:
    """A ``SandboxToolRunner`` that records the submission and returns a scripted run.

    The seam's own WS-event emission is exercised end-to-end by the real
    ``ChatSandboxToolRunner`` in ``test_sandbox_tool_runner.py``; this fake only proves
    the runtime *wiring* — that a ``run_python`` call reaches the seam when the tool
    is allowed.
    """

    def __init__(self, sandbox_ctx: object) -> None:
        self._ctx = sandbox_ctx
        self.run_id = uuid.uuid4()
        self.submissions: list[str] = []

    async def submit(self, *, code: str, timeout_s: int | None = None) -> object:
        from app.services.tools.types import SandboxRun

        self.submissions.append(code)
        return SandboxRun(
            code_run_id=self.run_id,
            status=CodeRunStatus.SUCCEEDED,
            exit_code=0,
            duration_ms=5,
            stdout="hi\n",
            stderr="",
            artifact_ids=(),
        )


def _sandbox_runtime(
    ctx: _Ctx,
    *,
    gateway: object,
    retrieval: object,
    backplane: InMemoryBackplane,
    sandbox_factory: object,
) -> ChatRuntime:
    return ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        principal=ctx.principal,
        request_id="req-1",
        source_ip="127.0.0.1",
        default_max_tool_turns=4,
        retrieval_factory=lambda _session: retrieval,  # type: ignore[arg-type,return-value]
        sandbox_factory=sandbox_factory,  # type: ignore[arg-type]
    )


async def test_run_python_seam_wired_when_allowed_but_gated_by_approval(ctx: _Ctx) -> None:
    """The seam is wired when a run offers run_python — but INV-7 still gates it.

    ``run_python`` is a T2 tool: the governed runner routes it through the approval
    gate BEFORE the handler runs, and the chat runtime wires the fail-closed default
    (``DenyAllApprovalGate`` — no in-session approval flow ships yet, F-ADMIN-TOOLS).
    So the seam is BUILT (run_python is in the allow-list) yet the call is
    **approval_denied** and the seam's ``submit`` is never reached — the consequential
    action does not execute without a recorded approval (the negative AC).
    """
    from app.services.assistant_runtime import AssistantRunConfig
    from app.services.prompts import GROUNDED_SYSTEM_PROMPT

    seam_holder: dict[str, _FakeSeam] = {}

    def _factory(sandbox_ctx: object) -> object:
        seam = _FakeSeam(sandbox_ctx)
        seam_holder["seam"] = seam
        return seam

    gateway = _ScriptedGateway(
        [
            # Turn 1: the model calls run_python.
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="run_python", arguments={"code": "print(1)"}),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            # Turn 2: the model answers (having read the denial).
            [StreamEvent(text="I could not run the code."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    import asyncio

    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _sandbox_runtime(
        ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane,
        sandbox_factory=_factory,
    )
    # An assistant config whose allow-list grants run_python (admin/assistant-gated).
    assistant_config = AssistantRunConfig(
        system_prompt=GROUNDED_SYSTEM_PROMPT,
        allowed=frozenset({"run_python"}),
        collection_ids=None,
        model=None,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="compute 1",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        assistant_config=assistant_config,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # The seam was BUILT (run_python was in the allow-list) — the wiring is present.
    assert "seam" in seam_holder
    # ...but the approval gate blocked the call, so the seam's submit was NOT reached
    # (INV-7: no unapproved consequential action executes).
    assert seam_holder["seam"].submissions == []
    # The tool_result event reports the governance denial the model reads.
    tool_results = [
        e for e in envs if e["type"] == "event" and e.get("name") == "tool_result"
    ]
    assert any(
        e["data"]["tool"] == "run_python" and e["data"]["ok"] is False  # type: ignore[index]
        for e in tool_results
    )


async def test_ad_hoc_session_never_wires_run_python_seam(ctx: _Ctx) -> None:
    """Deny-by-default: an ad-hoc session (default allow-list) never builds the seam."""
    factory = _RecordingSandboxFactory(seam=object())
    # The model tries to call run_python, but ad-hoc chat does not offer it, so the
    # governed runner denies it (not_permitted) and the seam factory is never invoked.
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="run_python", arguments={"code": "print(1)"}),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="No code ran."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    runtime = _sandbox_runtime(
        ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane,
        sandbox_factory=factory,
    )
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    # The factory was NEVER invoked — no execution plumbing on a path that can't use it.
    assert factory.contexts == []


# ---------------------------------------------------------------------------
# Custom-instructions injection (user settings) — composed BEFORE grounding.
# ---------------------------------------------------------------------------


class _CapturingGateway:
    """A gateway that records the ``messages`` of the first ``stream_tools`` call.

    Answers in a single tool-free turn so the run reaches the model exactly once with
    the composed system prompt as ``messages[0]`` — which the test inspects to assert
    the custom-instructions → grounding ordering.
    """

    def __init__(self) -> None:
        self.system_prompt: str | None = None
        self.first_messages: list[object] | None = None

    async def stream_tools(
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
        api_key: object = None,
        api_base: object = None,
    ) -> AsyncIterator[StreamEvent]:
        if self.system_prompt is None and isinstance(messages, list) and messages:
            # The runtime places the composed system prompt as the first ChatMessage.
            self.system_prompt = messages[0].content
            self.first_messages = list(messages)
        yield StreamEvent(text="ok")
        yield StreamEvent(finish_reason="stop")


async def test_custom_instructions_prepended_before_grounding(ctx: _Ctx) -> None:
    """The user's custom instructions lead the system prompt; grounding still follows."""
    from app.services.prompts import GROUNDED_SYSTEM_PROMPT

    gateway = _CapturingGateway()
    backplane = InMemoryBackplane()
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    instructions = "You are Alice's tax assistant. Always be concise."
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="hello",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        custom_instructions=instructions,
    )

    prompt = gateway.system_prompt
    assert prompt is not None
    # The custom instructions appear …
    assert instructions in prompt
    # … AND the grounding contract still follows (INV-3 preserved) …
    assert GROUNDED_SYSTEM_PROMPT in prompt
    # … in that order (instructions first, then grounding).
    assert prompt.index(instructions) < prompt.index(GROUNDED_SYSTEM_PROMPT)


async def test_no_custom_instructions_leaves_bare_grounded_prompt(ctx: _Ctx) -> None:
    """Ad-hoc chat with no custom instructions uses the grounded prompt unchanged."""
    from app.services.prompts import GROUNDED_SYSTEM_PROMPT

    gateway = _CapturingGateway()
    backplane = InMemoryBackplane()
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="hello",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        custom_instructions=None,
    )
    assert gateway.system_prompt == GROUNDED_SYSTEM_PROMPT


async def test_context_assembler_trims_history_end_to_end(ctx: _Ctx) -> None:
    """#410: the runtime assembles under a budget — a tiny window trims old history.

    Proves the assembler is actually wired into the runtime (not just unit-tested):
    with a small injected ``ContextConfig`` fallback window and no model in the
    map, the oldest turns are dropped before the prompt reaches the gateway, so
    the captured ``messages`` carry only the newest history + the question.
    """
    from app.domain.llm import ChatMessage
    from app.domain.llm import Role as LlmRole
    from app.llm.context import ContextConfig

    gateway = _CapturingGateway()
    backplane = InMemoryBackplane()
    # A moderate window (fallback 6000 − 1024 margin ⇒ ~4976 budget) that easily
    # holds the fixed segments (grounded prompt + tool schemas + question) but is
    # dwarfed by the two ENORMOUS ancient turns below — so the assembler trims
    # them and keeps the tiny recent turn, rather than refusing. The unknown model
    # id (not in the litellm map) exercises the fallback resolver (AC-3) too.
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        context_config=ContextConfig(fallback_max_input_tokens=6000, output_headroom_tokens=0),
    )
    long_history = [
        ChatMessage(role=LlmRole.USER, content="ancient question " * 3000),
        ChatMessage(role=LlmRole.ASSISTANT, content="ancient answer " * 3000),
        ChatMessage(role=LlmRole.USER, content="recent question"),
    ]
    await runtime.run(
        stream_id=uuid.uuid4().hex,
        session_id=ctx.session_id,
        question="the new question",
        model="some/unknown-model-not-in-map",
        history=long_history,
        collection_ids=None,
    )
    assert gateway.first_messages is not None
    contents = [m.content for m in gateway.first_messages]  # type: ignore[attr-defined]
    # The ancient, oversized turns were dropped; the new question is always last;
    # the grounded system prompt still leads (segment order preserved).
    assert "ancient question " * 3000 not in contents
    assert "ancient answer " * 3000 not in contents
    assert contents[-1] == "the new question"
    assert contents[0].startswith("You are Lumen Copilot")


class _RecordingSearchGateway:
    """Records the FULL wire estimate of every ``messages`` + ``tools`` payload.

    Drives the loop so tool results accumulate (#424 review, finding 1). Records
    each turn's estimate via the SAME assembler metric the guard uses (message
    wire form + tool schemas + framing) — not just content bytes — so a test can
    prove no over-budget payload is ever sent, and would FAIL if the guard were
    removed. Records whether the turn was a forced synthesis (tool_choice="none").
    """

    def __init__(self) -> None:
        self.estimates: list[int] = []
        self.synthesis_estimates: list[int] = []
        self.saw_compaction = False

    async def stream_tools(
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
        api_key: object = None,
        api_base: object = None,
    ) -> AsyncIterator[StreamEvent]:
        from app.llm.context import estimate_message_tokens

        assert isinstance(messages, list)
        assert isinstance(tools, list)
        if any("truncated to fit the context window" in m.content for m in messages):
            self.saw_compaction = True
        est = estimate_message_tokens(
            messages, tools, counter=lambda t: len(t.encode("utf-8"))
        )
        self.estimates.append(est)
        if tool_choice == "none":
            self.synthesis_estimates.append(est)
            yield StreamEvent(text="final")
            yield StreamEvent(finish_reason="stop")
            return
        yield StreamEvent(
            tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "x"}),),
            finish_reason="tool_calls",
        )


async def test_oversized_tool_results_never_exceed_budget(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#424 review, finding 1: with huge tool results and a tiny window, the loop
    never sends an over-budget payload — it refuses deterministically instead."""
    import asyncio
    import sys
    import types as _types

    from app.llm.context import ContextConfig

    # Force the assembler's counting/resolver onto their deterministic fallbacks
    # (conservative UTF-8 bytes; the configured fallback window) by making litellm
    # unavailable — so the token budget is measured in BYTES and the assertion
    # below is exact rather than dependent on a real tokenizer.
    def _raise(**_kw: object) -> int:
        raise RuntimeError("no tokenizer in test")

    fake_litellm = _types.SimpleNamespace(token_counter=_raise, get_model_info=_raise)
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)  # type: ignore[attr-defined]

    # A retrieval that returns a passage far larger than the window.
    huge = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="big.pdf",
        ord=0,
        text="P" * 8000,
        char_start=0,
        char_end=8000,
        score=0.9,
    )
    gateway = _RecordingSearchGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    # fallback 9072 − 0 headroom − 1024 margin ⇒ budget 8048 bytes. The initial
    # [system, question] + tool schemas (incl. ask_user since spec 0006/#429)
    # fits, but the accumulating tool results push a later turn over budget —
    # where the guard must refuse, not send.
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([huge]),
        backplane=backplane,
        context_config=ContextConfig(fallback_max_input_tokens=9072, output_headroom_tokens=0),
    )
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="summarize the big doc",
        model="some/unknown-model-not-in-map",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    budget = 9072 - 1024
    # Every payload the gateway received — measured by the SAME full-wire estimate
    # the guard uses — was within budget. Without the guard, the accumulating tool
    # results would push a later estimate over budget and this would FAIL.
    assert gateway.estimates  # the loop ran at least once (initial fit)
    assert all(e <= budget for e in gateway.estimates), gateway.estimates
    # And the guard actually fired: the run terminated with the typed refusal
    # rather than sending an over-budget call.
    types = [e["type"] for e in envs]
    assert types.count("done") + types.count("error") == 1
    terminal = envs[-1]
    assert terminal["type"] == "error"
    assert terminal["problem"]["code"] == "context_too_large"  # type: ignore[index]


async def test_forced_synthesis_payload_is_budget_guarded(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#424 third re-review: with a single tool turn, the FORCED-SYNTHESIS call
    must also be budget-guarded — a huge result gathered on the one turn cannot
    be sent unchecked at synthesis time."""
    import asyncio
    import sys
    import types as _types

    from app.llm.context import ContextConfig

    def _raise(**_kw: object) -> int:
        raise RuntimeError("no tokenizer in test")

    fake = _types.SimpleNamespace(token_counter=_raise, get_model_info=_raise)
    monkeypatch.setitem(sys.modules, "litellm", fake)  # type: ignore[arg-type]

    huge = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="big.pdf",
        ord=0,
        text="Q" * 8000,
        char_start=0,
        char_end=8000,
        score=0.9,
    )
    gateway = _RecordingSearchGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    # max_tool_turns=1: one tool turn, then the loop is exhausted and the runtime
    # makes the forced-synthesis call. Budget 4000 (5024 − 1024); the one huge
    # result makes the synthesis payload oversized ⇒ must be refused, not sent.
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([huge]),
        backplane=backplane,
        default_max_tool_turns=1,
        context_config=ContextConfig(fallback_max_input_tokens=5024, output_headroom_tokens=0),
    )
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="summarize the big doc",
        model="some/unknown-model-not-in-map",
        history=[],
        collection_ids=None,
    )
    await asyncio.wait_for(consumer, timeout=2.0)
    budget = 5024 - 1024
    # No payload (ordinary or forced-synthesis) exceeded budget.
    assert all(e <= budget for e in gateway.estimates), gateway.estimates


class _BigDocRetrieval(_FakeRetrieval):
    """A retrieval whose ``get_document`` returns a large document body.

    ``get_document`` results carry NO passages, so they are never auto-cited —
    the tier-1 (uncited) compaction surface, exactly like real ``get_document`` /
    web / code-output results (#415). Search returns nothing.
    """

    def __init__(self) -> None:
        super().__init__([])

    async def get_document(self, *, principal: object, document_id: object) -> DocumentText:
        return DocumentText(
            document_id=uuid.UUID(str(document_id)),
            document_name="big.pdf",
            text="D" * 5000,  # the tool caps the body at snippet_budget * 4
        )


class _RecordingGetDocGateway(_RecordingSearchGateway):
    """Like the search recorder, but every tool turn reads the big document."""

    def __init__(self, document_id: uuid.UUID) -> None:
        super().__init__()
        self._document_id = str(document_id)
        self.calls_made = 0

    async def stream_tools(  # type: ignore[override]
        self,
        messages: object,
        *,
        tools: object,
        model: object = None,
        tool_choice: object = None,
        api_key: object = None,
        api_base: object = None,
    ) -> AsyncIterator[StreamEvent]:
        from app.llm.context import estimate_message_tokens

        assert isinstance(messages, list)
        assert isinstance(tools, list)
        if any("truncated to fit the context window" in m.content for m in messages):
            self.saw_compaction = True
        est = estimate_message_tokens(messages, tools, counter=lambda t: len(t.encode("utf-8")))
        self.estimates.append(est)
        if tool_choice == "none":
            self.synthesis_estimates.append(est)
            yield StreamEvent(text="final")
            yield StreamEvent(finish_reason="stop")
            return
        yield StreamEvent(
            tool_calls=(
                ToolCall(
                    id=f"c{self.calls_made}",
                    name="get_document",
                    arguments={"document_id": self._document_id},
                ),
            ),
            finish_reason="tool_calls",
        )
        self.calls_made += 1


async def test_loop_compacts_old_results_and_answers_instead_of_refusing(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#415 AC-1 end-to-end: a long tool loop whose accumulating (UNCITED)
    ``get_document`` results overflow the window now DIGESTS the oldest ones (in
    chunks) and completes with a normal ``done`` answer — where pre-#415 the same
    scenario refused with context_too_large. (Search results carrying cited
    passages stay protected — that refusal case is pinned by
    ``test_oversized_tool_results_never_exceed_budget`` above.)"""
    import asyncio
    import sys
    import types as _types

    from app.llm.context import ContextConfig

    def _raise(**_kw: object) -> int:
        raise RuntimeError("no tokenizer in test")

    fake = _types.SimpleNamespace(token_counter=_raise, get_model_info=_raise)
    monkeypatch.setitem(sys.modules, "litellm", fake)  # type: ignore[arg-type]

    gateway = _RecordingGetDocGateway(ctx.document_id)
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    # Measured wire costs (byte counter): base (grounded prompt + question + the
    # 4 tool schemas) ≈ 3709; each get_document turn adds ≈ 2708 full / ≈ 573
    # digested (digest_chars=150). Budget 9000 (fallback 10024 − 1024 margin)
    # sits BETWEEN the 4-turn full requirement (~14541) and the digested floor
    # with the newest result whole (~8136): the loop MUST compact to proceed,
    # and compaction is sufficient — the pre-#415 code refused here.
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_BigDocRetrieval(),
        backplane=backplane,
        default_max_tool_turns=4,
        context_config=ContextConfig(
            fallback_max_input_tokens=10024,
            output_headroom_tokens=0,
            compaction_digest_chars=150,
            compaction_chunk_size=2,
        ),
    )
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="read the big doc",
        model="some/unknown-model-not-in-map",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    budget = 10024 - 1024
    # Every payload stayed within budget, compaction actually happened, and the
    # run completed with a normal answer — no context_too_large refusal.
    assert all(e <= budget for e in gateway.estimates), gateway.estimates
    assert gateway.saw_compaction  # old results were digested in-flight
    terminal = envs[-1]
    assert terminal["type"] == "done", terminal
    # The compacted transcript still produced answer deltas.
    assert any(e["type"] == "delta" for e in envs)


# --- #409: token & cache usage — reported on done, recorded per answer -------


async def test_usage_summed_reported_and_recorded_with_cache_fields(ctx: _Ctx) -> None:
    """#409 AC-1/AC-2: ``done.usage`` carries the cache fields summed across turns,
    and exactly one ``llm_usage`` row lands, linked to the session + assistant message."""
    import asyncio

    from app.db.repositories import LlmUsageRepository
    from app.domain.llm import TokenUsage

    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    retrieval = _FakeRetrieval([passage])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="search_text", arguments={"query": "deduction"}),
                    ),
                    finish_reason="tool_calls",
                    usage=TokenUsage(
                        prompt_tokens=100,
                        completion_tokens=10,
                        total_tokens=110,
                        cached_prompt_tokens=0,
                        cache_write_tokens=80,
                    ),
                ),
            ],
            [
                StreamEvent(text="The 2024 standard deduction is $14,600."),
                StreamEvent(
                    finish_reason="stop",
                    usage=TokenUsage(
                        prompt_tokens=120,
                        completion_tokens=20,
                        total_tokens=140,
                        cached_prompt_tokens=90,
                        cache_write_tokens=0,
                    ),
                ),
            ],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
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

    done = envs[-1]
    assert done["type"] == "done"
    usage = done["data"]["usage"]  # type: ignore[index]
    assert usage == {
        "promptTokens": 220,
        "completionTokens": 30,
        "totalTokens": 250,
        "cachedPromptTokens": 90,
        "cacheWriteTokens": 80,
    }

    # AC-2: one recorded row per answer, tenant-scoped, linked to the message.
    start = envs[0]
    message_id = uuid.UUID(start["data"]["messageId"])  # type: ignore[index]
    async with ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.message_id == message_id
        assert row.model == "anthropic/claude-opus-4.8"
        assert row.prompt_tokens == 220
        assert row.completion_tokens == 30
        assert row.total_tokens == 250
        assert row.cached_prompt_tokens == 90
        assert row.cache_write_tokens == 80


async def test_usage_row_zeroed_when_provider_omits_usage_and_is_tenant_scoped(
    ctx: _Ctx,
) -> None:
    """#409 AC-3/AC-4 negatives: no provider usage ⇒ zeroed fields but the row still
    lands (never a crash); a foreign tenant's repository sees nothing (INV-1)."""
    import asyncio

    from app.db.repositories import LlmUsageRepository

    gateway = _ScriptedGateway(
        [[StreamEvent(text="An answer."), StreamEvent(finish_reason="stop")]]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="hello",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)
    assert envs[-1]["type"] == "done"

    async with ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.prompt_tokens == 0
        assert row.completion_tokens == 0
        assert row.total_tokens == 0
        assert row.cached_prompt_tokens == 0
        assert row.cache_write_tokens == 0

        # INV-1: a repository bound to another tenant must see nothing.
        foreign = await LlmUsageRepository(session, uuid.uuid4()).list_for_session(ctx.session_id)
        assert foreign == []


# --- Spec 0006 (#429): steps, ask_user, suggestions --------------------------


def _steps(envs: list[dict[str, object]]) -> list[tuple[object, object]]:
    """The (key, state) sequence of the stream's step events."""
    return [
        (e["data"]["key"], e["data"]["state"])  # type: ignore[index]
        for e in envs
        if e["type"] == "event" and e.get("name") == "step"
    ]


def _ask_user_call(call_id: str = "a1", **overrides: object) -> ToolCall:
    arguments: dict[str, object] = {
        "question": "Which quarter did you mean?",
        "options": [
            {"label": "Q1 2026", "description": "January through March"},
            {"label": "Q2 2026"},
        ],
        "allow_free_text": False,
    }
    arguments.update(overrides)
    return ToolCall(id=call_id, name="ask_user", arguments=arguments)


async def test_step_events_bracket_the_run(ctx: _Ctx) -> None:
    """AC-1: prepare/think/finalize phases bracket a grounded answer, in order."""
    import asyncio

    passage = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="search_text", arguments={"query": "deduction"}),
                    ),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="Answer."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx, gateway=gateway, retrieval=_FakeRetrieval([passage]), backplane=backplane
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    assert _steps(envs) == [
        ("prepare", "started"),
        ("prepare", "completed"),
        ("think", "started"),
        ("think", "completed"),
        ("think", "started"),
        ("think", "completed"),
        ("finalize", "started"),
        ("finalize", "completed"),
    ]
    # Suggestions are OPT-IN: none generated, no suggest step, no chat() call.
    assert gateway.chat_calls == 0
    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "suggestions" not in names
    # The tool turn's think step reports what it requested; turns are ordinal.
    step_data = [
        e["data"]
        for e in envs
        if e["type"] == "event" and e.get("name") == "step"
    ]
    think_started = [
        d for d in step_data if d["key"] == "think" and d["state"] == "started"  # type: ignore[index]
    ]
    assert [d["turn"] for d in think_started] == [1, 2]  # type: ignore[index]
    completed_details = [
        d.get("detail")  # type: ignore[union-attr]
        for d in step_data
        if d["key"] == "think" and d["state"] == "completed"  # type: ignore[index]
    ]
    assert completed_details == ["requested 1 tool", None]


async def test_ask_user_ends_turn_as_question(ctx: _Ctx) -> None:
    """AC-2: a valid ask_user persists the question (zero citations) and ends
    the stream with event:ask_user + done(finishReason=ask_user)."""
    import asyncio

    gateway = _ScriptedGateway(
        [[StreamEvent(tool_calls=(_ask_user_call(),), finish_reason="tool_calls")]]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="How did we do?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # Exactly one ask_user event, carrying the contract payload.
    asks = [e for e in envs if e["type"] == "event" and e.get("name") == "ask_user"]
    assert len(asks) == 1
    data = asks[0]["data"]
    assert data["question"] == "Which quarter did you mean?"  # type: ignore[index]
    assert data["allowFreeText"] is False  # type: ignore[index]
    assert data["options"] == [  # type: ignore[index]
        {"label": "Q1 2026", "description": "January through March"},
        {"label": "Q2 2026"},
    ]
    # The turn ended as a question: no answer deltas, no tool execution events,
    # no citations, and the terminal reports finishReason=ask_user.
    assert not [e for e in envs if e["type"] == "delta"]
    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "tool_call" not in names and "tool_result" not in names and "citation" not in names
    done = envs[-1]
    assert done["type"] == "done"
    assert done["data"]["finishReason"] == "ask_user"  # type: ignore[index]
    assert done["data"]["citationCount"] == 0  # type: ignore[index]
    assert data["messageId"] == done["data"]["messageId"]  # type: ignore[index]

    # Persisted: the question text IS the message; the structured payload
    # round-trips through the repository for the reload path (Message.question).
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        assistant = [m for m in messages if m.role.value == "assistant"]
        assert len(assistant) == 1
        stored = assistant[0]
        assert stored.content == "Which quarter did you mean?"
        assert stored.question is not None
        assert [o.label for o in stored.question.options] == ["Q1 2026", "Q2 2026"]
        assert stored.question.options[0].description == "January through March"
        assert stored.question.allow_free_text is False
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message_hydrated(
            stored.id
        )
        assert citations == []


async def test_ask_user_mixed_batch_executes_nothing(ctx: _Ctx) -> None:
    """Spec 0006 §2: a valid ask_user in a batch wins — no other call executes."""
    import asyncio

    retrieval = _FakeRetrieval([_passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")])
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="c1", name="search_text", arguments={"query": "x"}),
                        _ask_user_call("a2"),
                    ),
                    finish_reason="tool_calls",
                )
            ]
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    assert retrieval.queries == []  # the search in the same batch never ran
    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "tool_call" not in names
    assert "ask_user" in names
    assert envs[-1]["data"]["finishReason"] == "ask_user"  # type: ignore[index]


async def test_ask_user_malformed_recovers_to_normal_answer(ctx: _Ctx) -> None:
    """AC-N1: malformed ask_user becomes a typed tool_bad_args result the model
    reads; the loop continues to a normal answer with no question persisted."""
    import asyncio

    gateway = _ScriptedGateway(
        [
            # Turn 1: ask_user with a single option -- structurally invalid.
            [
                StreamEvent(
                    tool_calls=(_ask_user_call("bad", options=[{"label": "Only one"}]),),
                    finish_reason="tool_calls",
                )
            ],
            # Turn 2: the model recovers with a normal answer.
            [StreamEvent(text="Recovered answer."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    # The rejected call surfaced as an ordinary governed tool result (ok=False,
    # tool_bad_args) -- never as an ask_user event or a broken stream.
    results = [e for e in envs if e["type"] == "event" and e.get("name") == "tool_result"]
    assert len(results) == 1
    assert results[0]["data"]["ok"] is False  # type: ignore[index]
    assert results[0]["data"]["error"] == "tool_bad_args"  # type: ignore[index]
    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "ask_user" not in names
    done = envs[-1]
    assert done["type"] == "done"
    assert done["data"]["finishReason"] == "stop"  # type: ignore[index]
    text = "".join(e["data"]["text"] for e in envs if e["type"] == "delta")  # type: ignore[index]
    assert text == "Recovered answer."
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        assistant = [m for m in messages if m.role.value == "assistant"]
        assert len(assistant) == 1
        assert assistant[0].content == "Recovered answer."
        assert assistant[0].question is None


async def test_ask_user_not_intercepted_when_non_interactive(ctx: _Ctx) -> None:
    """Headless/preview posture: a VALID ask_user is not intercepted -- the
    governed handler refuses and the model proceeds to an answer."""
    import asyncio

    gateway = _ScriptedGateway(
        [
            [StreamEvent(tool_calls=(_ask_user_call(),), finish_reason="tool_calls")],
            [StreamEvent(text="Best-guess answer."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane, interactive=False
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "ask_user" not in names
    results = [e for e in envs if e["type"] == "event" and e.get("name") == "tool_result"]
    assert len(results) == 1
    assert results[0]["data"]["ok"] is False  # type: ignore[index]
    assert envs[-1]["data"]["finishReason"] == "stop"  # type: ignore[index]
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        assistant = [m for m in messages if m.role.value == "assistant"]
        assert assistant[0].content == "Best-guess answer."
        assert assistant[0].question is None


async def test_suggestions_emitted_after_answer(ctx: _Ctx) -> None:
    """AC-3: one suggestions event after the final delta, before done; its
    usage folds into the answer's llm_usage row (#409)."""
    import asyncio

    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(text="The answer."),
                StreamEvent(
                    finish_reason="stop",
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                ),
            ]
        ],
        chat_completion=Completion(
            content='["What changed vs Q1?", "Who owns this?", "What changed vs Q1?"]',
            model="anthropic/claude-opus-4.8",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
        ),
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        suggestions_enabled=True,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    assert gateway.chat_calls == 1
    sugg = [e for e in envs if e["type"] == "event" and e.get("name") == "suggestions"]
    assert len(sugg) == 1
    # Deduped (case-insensitive), order-preserving, capped by config.
    assert sugg[0]["data"]["suggestions"] == [  # type: ignore[index]
        "What changed vs Q1?",
        "Who owns this?",
    ]
    done = envs[-1]
    assert done["type"] == "done"
    assert sugg[0]["data"]["messageId"] == done["data"]["messageId"]  # type: ignore[index]
    # Ordering: after the last delta, before the terminal.
    last_delta = max(i for i, e in enumerate(envs) if e["type"] == "delta")
    assert envs.index(sugg[0]) > last_delta
    # suggest step bracketed the call.
    assert ("suggest", "started") in _steps(envs)
    assert ("suggest", "completed") in _steps(envs)
    # The nicety's tokens are accounted in the done usage (and the llm_usage row).
    assert done["data"]["usage"]["totalTokens"] == 27  # type: ignore[index]
    from app.db.repositories import LlmUsageRepository

    async with ctx.sessionmaker() as session:
        rows = await LlmUsageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assert len(rows) == 1
        assert rows[0].total_tokens == 27


async def test_suggestions_failure_is_silent(ctx: _Ctx) -> None:
    """AC-N2: a failing suggestions call changes nothing -- no event, no error."""
    import asyncio

    gateway = _ScriptedGateway(
        [[StreamEvent(text="The answer."), StreamEvent(finish_reason="stop")]],
        chat_completion=None,  # chat() raises -- the nicety must be swallowed
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        suggestions_enabled=True,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    assert gateway.chat_calls == 1
    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "suggestions" not in names
    assert envs[-1]["type"] == "done"  # the answer was untouched by the failure
    # The suggest step still closed -- no orphaned spinner.
    assert ("suggest", "completed") in _steps(envs)


async def test_ask_user_turn_skips_suggestions(ctx: _Ctx) -> None:
    """Spec 0006: a question turn generates no follow-ups (the options ARE the
    suggestions)."""
    import asyncio

    gateway = _ScriptedGateway(
        [[StreamEvent(tool_calls=(_ask_user_call(),), finish_reason="tool_calls")]],
        chat_completion=Completion(content='["never used"]', model="m"),
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        suggestions_enabled=True,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)

    assert gateway.chat_calls == 0
    names = [e.get("name") for e in envs if e["type"] == "event"]
    assert "suggestions" not in names
    assert "ask_user" in names


def test_parse_suggestions_accepts_fenced_and_prose_json() -> None:
    from app.services.chat_runtime import _parse_suggestions

    fenced = '```json\n["A?", "B?"]\n```'
    assert _parse_suggestions(fenced, limit=3) == ["A?", "B?"]
    prose = 'Here you go: ["One?", {"question": "Two?"}] -- enjoy.'
    assert _parse_suggestions(prose, limit=3) == ["One?", "Two?"]
    assert _parse_suggestions("no json here", limit=3) == []
    assert _parse_suggestions('["a?", "A?", "b?", "c?", "d?"]', limit=3) == ["a?", "b?", "c?"]


def test_ask_user_parse_bounds() -> None:
    from app.domain.chat import AskUserQuestion, AskUserValidationError

    # String shorthand + dict options both accepted; labels deduped case-insensitively.
    q = AskUserQuestion.parse(
        {"question": "Pick one", "options": ["Alpha", {"label": "alpha"}, {"label": "Beta"}]}
    )
    assert [o.label for o in q.options] == ["Alpha", "Beta"]
    assert q.allow_free_text is True
    with pytest.raises(AskUserValidationError):
        AskUserQuestion.parse({"question": "", "options": ["A", "B"]})
    with pytest.raises(AskUserValidationError):
        AskUserQuestion.parse({"question": "Pick", "options": ["Only"]})
    with pytest.raises(AskUserValidationError):
        AskUserQuestion.parse({"question": "Pick", "options": ["A", "B", "C", "D", "E"]})
    with pytest.raises(AskUserValidationError):
        AskUserQuestion.parse({"question": "Pick", "options": "not a list"})


# --- Spec 0007 (#432): usage endpoint math + pinned-document narrowing -------


async def test_session_usage_totals_and_last(ctx: _Ctx) -> None:
    """AC-1: totals sum every answer's row; last is the newest; empty is zeroes."""
    from app.db.repositories import LlmUsageRepository

    async with ctx.sessionmaker() as session:
        repo = LlmUsageRepository(session, ctx.tenant_id)
        empty = await repo.totals_for_session(ctx.session_id)
        assert (empty.answers, empty.total_tokens) == (0, 0)
        assert await repo.last_for_session(ctx.session_id) is None
        await repo.record(
            model="m",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cached_prompt_tokens=10,
            session_id=ctx.session_id,
        )
        await repo.record(
            model="m",
            prompt_tokens=300,
            completion_tokens=30,
            total_tokens=330,
            cache_write_tokens=5,
            session_id=ctx.session_id,
        )
        await session.commit()
        totals = await repo.totals_for_session(ctx.session_id)
        assert totals.answers == 2
        assert totals.prompt_tokens == 400
        assert totals.completion_tokens == 50
        assert totals.total_tokens == 450
        assert totals.cached_prompt_tokens == 10
        assert totals.cache_write_tokens == 5
        last = await repo.last_for_session(ctx.session_id)
        assert last is not None and last.prompt_tokens == 300
        # INV-1: a foreign-tenant repository sees nothing.
        foreign = await LlmUsageRepository(session, uuid.uuid4()).totals_for_session(
            ctx.session_id
        )
        assert foreign.answers == 0


def test_input_budget_for_model_matches_assembler_formula() -> None:
    """The meter reports the assembler's own arithmetic (spec 0007 §2)."""
    from app.llm.context import ContextConfig, input_budget_for_model

    cfg = ContextConfig(fallback_max_input_tokens=50_000, output_headroom_tokens=8_000)
    budget, known = input_budget_for_model("m", cfg, resolver=lambda _m: 200_000)
    assert (budget, known) == (200_000 - 8_000 - 1_024, True)
    fallback_budget, fallback_known = input_budget_for_model(
        "unknown", cfg, resolver=lambda _m: None
    )
    assert (fallback_budget, fallback_known) == (50_000 - 8_000 - 1_024, False)


async def test_pinned_document_ids_reach_retrieval(ctx: _Ctx) -> None:
    """AC-4: run(document_ids=...) threads through ToolContext into search_text,
    and the model-visible question carries the count-only pinned note."""
    import asyncio

    class _PinRecordingRetrieval(_FakeRetrieval):
        def __init__(self) -> None:
            super().__init__([])
            self.document_ids: list[object] = []

        async def search_text(
            self,
            *,
            principal: object,
            query: str,
            k: int,
            collection_ids: object = None,
            document_ids: object = None,
        ) -> list[RetrievedPassage]:
            self.document_ids.append(document_ids)
            return []

    class _PromptRecordingGateway(_ScriptedGateway):
        def __init__(self, turns: list[list[StreamEvent]]) -> None:
            super().__init__(turns)
            self.first_prompt: object = None

        async def stream_tools(  # type: ignore[override]
            self,
            messages: object,
            *,
            tools: object,
            model: object = None,
            tool_choice: object = None,
            api_key: object = None,
            api_base: object = None,
        ):
            if self.first_prompt is None:
                self.first_prompt = list(messages)  # type: ignore[call-overload]
            async for ev in super().stream_tools(
                messages,
                tools=tools,
                model=model,
                tool_choice=tool_choice,
                api_key=api_key,
                api_base=api_base,
            ):
                yield ev

    pinned = [uuid.uuid4(), uuid.uuid4()]
    retrieval = _PinRecordingRetrieval()
    gateway = _PromptRecordingGateway(
        [
            [
                StreamEvent(
                    tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "x"}),),
                    finish_reason="tool_calls",
                )
            ],
            [StreamEvent(text="Scoped answer."), StreamEvent(finish_reason="stop")],
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(ctx, gateway=gateway, retrieval=retrieval, backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="what does it say?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
        document_ids=list(pinned),
    )
    envs = await asyncio.wait_for(consumer, timeout=2.0)
    assert envs[-1]["type"] == "done"

    # The pinned ids reached the retrieval chokepoint via the ToolContext.
    assert retrieval.document_ids == [list(pinned)]
    # The ASSEMBLED question carries the count-only note; the persisted user
    # content and the audit hash use the raw question (asserted via prompt only
    # here — persistence of user turns is the send path, not the runtime).
    prompt = gateway.first_prompt
    assert prompt is not None
    question_msg = prompt[-1]
    assert "attached 2 specific document(s)" in question_msg.content
    assert question_msg.content.startswith("what does it say?")


def test_hybrid_body_carries_document_terms_in_both_legs() -> None:
    """AC-4/AC-N1: the pinned filter is ANDed into BOTH hybrid legs."""
    from app.search.filters import SearchAllowFilter
    from app.search.store import _hybrid_body

    tenant = uuid.uuid4()
    doc = uuid.uuid4()
    body = _hybrid_body(
        query_text="q",
        embedding=[0.1, 0.2],
        allow=SearchAllowFilter(tenant_id=tenant, owner_ids=frozenset({uuid.uuid4()})),
        k=5,
        document_ids=[doc],
    )
    legs = body["query"]["hybrid"]["queries"]
    bm25_filters = legs[0]["bool"]["filter"]
    knn_filters = legs[1]["knn"]["embedding"]["filter"]["bool"]["filter"]
    expected = {"terms": {"document_id": [str(doc)]}}
    assert expected in bm25_filters
    assert expected in knn_filters
    # And absent when not pinned (no behavior change).
    unpinned = _hybrid_body(
        query_text="q",
        embedding=[0.1, 0.2],
        allow=SearchAllowFilter(tenant_id=tenant, owner_ids=frozenset({uuid.uuid4()})),
        k=5,
    )
    assert expected not in unpinned["query"]["hybrid"]["queries"][0]["bool"]["filter"]
